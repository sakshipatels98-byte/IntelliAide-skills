#!/usr/bin/env python3
"""
live-cluster-extraction.py — extract live cluster state using kubernetes.dynamic.DynamicClient.

No hardcoded OpenShift/Kubernetes API versions or kinds: paths drive discovery.
  - cluster-scoped-resources/<api-group>/<plural>.yaml
  - namespaces/<ns>/<api-group-or-alias>/<plural>.yaml

API group aliases (cluster extraction layout only — not resource-specific):
  - folder name "core" → core Kubernetes API (empty API group, /api/v1/...)

Discovery: LazyDiscoverer.__iter__ yields *lists* of Resource per kind — we flatten them.
  Fallback: resources.search(group=..., name=<plural>) loads that API group lazily.
  prefer .preferred,must_gather_selector then try LIST until one succeeds (handles multi-version CRDs).

Pod logs: discovered Pod resource + GET .../pods/{name}/log
etcd_info/*.json: EtcdClient (etcd_client.py) — exec etcdctl in a running etcd pod.
host_service_logs/...: NodeLogsClient (node_logs_client.py) — node proxy journal logs.
Other etcd_info names: optional ETCD_CMD_* env + legacy exec fallback.

Errors / retries:
  - Path in file_paths but API group/plural not discoverable → NOT_PRESENT_ON_CLUSTER (clear message).
  - Resource found via discovery but LIST/GET fails with transient errors → retries
    (MG_RETRIEVE_RETRIES, MG_RETRIEVE_DELAY or --retrieve-retries / --retrieve-delay).

Requires: pip install kubernetes pyyaml
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import sys
import time
from pathlib import Path

# Ensure python-client is on path for etcd_client, node_logs_client, inspect_kube_adapter.
# Walk up from this file until we find python-client/etcd_client.py (robust if cwd/layout varies).
_here = Path(__file__).resolve().parent
for _base in (_here, *_here.parents):
    _python_client = _base / "python-client"
    if (_python_client / "etcd_client.py").is_file():
        _pc = str(_python_client)
        if _pc not in sys.path:
            sys.path.insert(0, _pc)
        break

import yaml

from kubernetes import client, config
from kubernetes.client import ApiClient
from kubernetes.dynamic.exceptions import ResourceNotFoundError, ResourceNotUniqueError
from kubernetes.dynamic import DynamicClient
from kubernetes.stream import stream

from etcd_client import ETCD_INFO_COMMANDS, EtcdClient
from inspect_kube_adapter.client import KubeClientError
from node_logs_client import NodeLogsClient

# ---------------------------------------------------------------------------
# Retries when discovery finds the resource but LIST/GET fails transiently.
# MG_RETRIEVE_RETRIES (default 3), MG_RETRIEVE_DELAY base seconds (default 1.0).
# ---------------------------------------------------------------------------


# How many times to retry one API call before giving up (from env, minimum 1).
def _retrieve_max_attempts() -> int:
    return max(1, int(os.environ.get("MG_RETRIEVE_RETRIES", "3")))


# Base seconds between retries; multiplied by attempt number for backoff (from env).
def _retrieve_base_delay() -> float:
    return max(0.0, float(os.environ.get("MG_RETRIEVE_DELAY", "1.0")))


# Error type: the path asks for an API resource this cluster does not advertise in discovery.
class ResourceAbsentOnCluster(Exception):
    # Build a clear message naming the path, group, plural, and cluster vs namespace scope.
    def __init__(
        self,
        logical_path: str,
        group: str,
        plural: str,
        namespaced: bool,
    ) -> None:
        self.logical_path = logical_path
        self.group = group
        self.plural = plural
        self.namespaced = namespaced
        scope = "namespaced" if namespaced else "cluster-scoped"
        super().__init__(
            f"API resource not present on the live cluster ({scope}): "
            f"group={group!r} plural={plural!r}. "
            f"Path {logical_path!r} is listed in file_paths but this resource is not "
            f"registered or discoverable via the Kubernetes/OpenShift API on this cluster."
        )


# Error type: discovery found the resource type, but list/get still failed after retries.
class RetrievalFailedAfterRetries(Exception):
    pass


# Return True if this exception looks like a temporary network/server problem (retry it).
def _is_retriable_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, BrokenPipeError)):
        return True
    if isinstance(exc, OSError):
        errn = getattr(exc, "errno", None)
        if errn in (
            errno.ECONNRESET,
            errno.ECONNREFUSED,
            errno.ETIMEDOUT,
            errno.EPIPE,
            errno.EAGAIN,
        ):
            return True
    if isinstance(exc, client.ApiException):
        s = exc.status
        if s is None:
            return True
        if s in (408, 425, 429, 500, 502, 503, 504):
            return True
        return False
    msg = str(exc).lower()
    if any(
        w in msg
        for w in (
            "timeout",
            "timed out",
            "connection reset",
            "connection aborted",
            "temporarily unavailable",
            "try again",
        )
    ):
        return True
    return False


# Call operation() repeatedly: sleep and retry only on retriable errors until max attempts.
def call_with_retry(operation) -> object:
    max_attempts = _retrieve_max_attempts()
    base_delay = _retrieve_base_delay()
    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as e:  # noqa: BLE001
            last = e
            if attempt >= max_attempts or not _is_retriable_exception(e):
                raise
            if base_delay > 0:
                time.sleep(base_delay * attempt)
    assert last is not None
    raise last


# ---------------------------------------------------------------------------
# Must-gather folder name → Kubernetes API group string (discovery key).
# Everything else in the path is treated as a literal API group (e.g. config.openshift.io).
# ---------------------------------------------------------------------------

# Map a path folder segment to a Kubernetes API group ("core" means built-in /api/v1).
def api_group_from_path_segment(segment: str) -> str:
    if segment == "core":
        return ""
    return segment


# ---------------------------------------------------------------------------
# Parse file_paths.md
# ---------------------------------------------------------------------------


# Read markdown list of paths, strip noise, return plain relative path strings.
def parse_paths_from_md(md_path: Path) -> list[str]:
    text = md_path.read_text(encoding="utf-8", errors="replace")
    paths: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^\d+\.\s*", "", line)
        line = re.sub(r"^\[[^\]]+\]\s*", "", line)
        for sep in (" — ", " – ", " - "):
            if sep in line:
                line = line.split(sep, 1)[0].strip()
                break
        line = line.strip().strip("`")
        line = line.replace("namesapces/", "namespaces/")
        if line:
            paths.append(line)
    return paths


# Return the API group for a dynamic client Resource (empty string = core).
def _resource_group(res) -> str:
    return getattr(res, "group", None) or ""


# True if this is a Kubernetes *List kind (metadata type), not a normal resource kind.
def _is_list_kind(res) -> bool:
    k = getattr(res, "kind", None) or ""
    return str(k).endswith("List")


# Yield each Resource from discovery, flattening inner lists the client sometimes returns.
def _iterate_flat_resources(dyn: DynamicClient):
    for bucket in dyn.resources:
        if isinstance(bucket, list):
            for res in bucket:
                yield res
        else:
            yield bucket


# Find all discovery Resource entries that match group, plural name, and cluster vs namespaced.
def collect_matching_resources(
    dyn: DynamicClient,
    group: str,
    plural: str,
    namespaced: bool,
) -> list:
    found: list = []
    for res in _iterate_flat_resources(dyn):
        if _is_list_kind(res):
            continue
        if getattr(res, "name", None) != plural:
            continue
        if _resource_group(res) != group:
            continue
        if bool(res.namespaced) != bool(namespaced):
            continue
        found.append(res)
    return found


# Ask the client to search one API group by plural name; return matching Resource objects.
def _search_resources_by_group_plural(
    dyn: DynamicClient,
    group: str,
    plural: str,
    namespaced: bool,
) -> list:
    try:
        results = dyn.resources.search(group=group, name=plural)
    except ResourceNotFoundError:
        return []
    if not results:
        return []
    out: list = []
    for res in results:
        if isinstance(res, list):
            for r in res:
                if not _is_list_kind(r) and bool(r.namespaced) == bool(namespaced):
                    if getattr(r, "name", None) == plural and _resource_group(r) == group:
                        out.append(r)
        elif not _is_list_kind(res):
            if bool(res.namespaced) != bool(namespaced):
                continue
            if getattr(res, "name", None) == plural and _resource_group(res) == group:
                out.append(res)
    return out


def pick_resources_to_try(dyn: DynamicClient, group: str, plural: str, namespaced: bool) -> list:
    matches = collect_matching_resources(dyn, group, plural, namespaced)
    if not matches:
        matches = _search_resources_by_group_plural(dyn, group, plural, namespaced)
    if not matches and not group:
        # Core v1: search may need explicit api group '' ; try get by plural only paths
        try:
            r = dyn.resources.get(api_version="v1", name=plural)
            if bool(r.namespaced) == bool(namespaced):
                matches = [r]
        except (ResourceNotFoundError, ResourceNotUniqueError):
            try:
                alt = dyn.resources.search(api_version="v1", name=plural)
                matches = [
                    x
                    for x in alt
                    if not isinstance(x, list)
                    and not _is_list_kind(x)
                    and bool(x.namespaced) == bool(namespaced)
                    and getattr(x, "name", None) == plural
                ]
            except ResourceNotFoundError:
                matches = []
    if not matches:
        raise ResourceNotFoundError(
            f"No API resource for group={group!r} plural={plural!r} namespaced={namespaced}"
        )
    # Deduplicate (flatten + search can overlap)
    seen: set[tuple] = set()
    uniq: list = []
    for r in matches:
        key = (_resource_group(r), r.api_version, r.kind, r.name)
        if key not in seen:
            seen.add(key)
            uniq.append(r)
    matches = uniq
    # preferred first, then stable sort by api_version for repeatability
    matches.sort(
        key=lambda r: (
            not getattr(r, "preferred", False),
            r.api_version or "",
        )
    )
    return matches


# List all objects for a resource type (all pages) and return a single dict like a YAML List.
def list_collection_to_dict(dyn: DynamicClient, resource, namespace: str | None) -> dict:
    merged_items: list = []
    api_version = None
    kind = None
    _continue = None
    while True:
        kw: dict = {}
        if namespace is not None:
            kw["namespace"] = namespace
        if _continue:
            kw["_continue"] = _continue
        inst = resource.get(**kw)
        body = inst.to_dict()
        api_version = body.get("apiVersion") or api_version
        kind = body.get("kind") or kind
        merged_items.extend(body.get("items") or [])
        meta = body.get("metadata") or {}
        _continue = meta.get("continue")
        if not _continue:
            break
    base_kind = (kind or "Unknown").replace("List", "")
    return {
        "apiVersion": api_version or resource.api_version,
        "kind": kind or f"{base_kind}List",
        "items": merged_items,
        "metadata": {"remainingItemCount": None},
    }


# Discover the right API, list objects with retries, write YAML; raise clear errors if impossible.
def extract_list_with_discovery(
    dyn: DynamicClient,
    group: str,
    plural: str,
    namespaced: bool,
    namespace: str | None,
    dest: Path,
    logical_path: str,
) -> str:
    try:
        candidates = pick_resources_to_try(dyn, group, plural, namespaced)
    except ResourceNotFoundError:
        raise ResourceAbsentOnCluster(logical_path, group, plural, namespaced) from None

    errs: list[str] = []
    for r in candidates:
        gv = f"{_resource_group(r) or 'core'}/{r.api_version}"

        # One-shot LIST for this API version (wrapped by call_with_retry).
        def _do_list():
            return list_collection_to_dict(dyn, r, namespace)

        try:
            data = call_with_retry(_do_list)
            write_yaml(dest, data)
            return f"OK LIST {gv} kind={r.kind} plural={plural}"
        except (ResourceNotFoundError, ResourceNotUniqueError, client.ApiException) as e:
            errs.append(f"{gv}: {e}")
        except Exception as e:  # noqa: BLE001
            errs.append(f"{gv}: {e}")

    n = _retrieve_max_attempts()
    raise RetrievalFailedAfterRetries(
        f"Resource exists in API discovery for path {logical_path!r} but LIST failed "
        f"after up to {n} attempt(s) per API version: " + " | ".join(errs)
    )


# Write Python dict or API object to a YAML file, creating parent folders if needed.
def write_yaml(dest: Path, data) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(data, "to_dict"):
        data = data.to_dict()
    dest.write_text(
        yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


# For one namespace, list pods and save each container log (current or previous) under out_root.
def extract_pod_logs_dynamic(
    dyn: DynamicClient,
    namespace: str,
    out_root: Path,
    previous: bool,
    logical_path: str = "",
) -> list[str]:
    report: list[str] = []
    lp = logical_path or f"namespaces/{namespace}/pods/*/logs/"
    try:
        pod_candidates = pick_resources_to_try(dyn, "", "pods", namespaced=True)
    except ResourceNotFoundError:
        raise ResourceAbsentOnCluster(lp, "", "pods", True) from None
    pod_r = pod_candidates[0]

    # List Pod objects in the namespace (retried on transient failures).
    def _do_list_pods():
        return list_collection_to_dict(dyn, pod_r, namespace)

    try:
        pods = call_with_retry(_do_list_pods)
    except Exception as e:  # noqa: BLE001
        n = _retrieve_max_attempts()
        raise RetrievalFailedAfterRetries(
            f"Pods API exists in discovery for {lp!r} but listing pods in namespace "
            f"{namespace!r} failed after up to {n} attempt(s): {e}"
        ) from e

    suffix = "previous.log" if previous else "current.log"
    for item in pods.get("items") or []:
        meta = item.get("metadata") or {}
        pname = meta.get("name")
        if not pname:
            continue
        containers: list[str] = []
        for c in (item.get("spec") or {}).get("containers") or []:
            if c.get("name"):
                containers.append(c["name"])
        for ctr in containers:
            rel = Path("namespaces") / namespace / "pods" / pname / ctr / "logs" / suffix
            dest = out_root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            path = f"/api/v1/namespaces/{namespace}/pods/{pname}/log"
            q = [("container", ctr)]
            if previous:
                q.append(("previous", "true"))

            # HTTP GET pod log for one container (retried on transient failures).
            def _do_log(
                _path: str = path,
                _q: list = q,
            ):
                return dyn.request(
                    "get",
                    _path,
                    query_params=_q,
                    serialize=False,
                    _preload_content=False,
                )

            try:
                resp = call_with_retry(_do_log)
                raw = resp.data
                if isinstance(raw, bytes):
                    text = raw.decode("utf-8", errors="replace")
                else:
                    text = str(raw)
                dest.write_text(text, encoding="utf-8")
                report.append(f"OK GET log → {rel}")
            except Exception as e:  # noqa: BLE001
                dest.write_text(f"# ERROR pod log: {e}\n", encoding="utf-8")
                report.append(f"ERR {rel}: {e}")
    return report


# Use EtcdClient: run etcdctl in openshift-etcd (or --etcd-namespace); write stdout to dest.
def extract_etcd_via_client(
    dest: Path,
    logical_path: str,
    api: ApiClient,
    etcd_namespace: str,
) -> str:
    client_inst = EtcdClient(api, namespace=etcd_namespace)

    def _run():
        return client_inst.execute(logical_path)

    try:
        out = call_with_retry(_run)
    except KubeClientError as e:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            json.dumps({"error": str(e), "path": logical_path}),
            encoding="utf-8",
        )
        return f"ERR EtcdClient {e}"

    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(out, encoding="utf-8")
    return "OK EtcdClient"


# Fetch systemd journal logs from nodes (masters/workers) via node proxy API; write plain text.
def extract_host_service_logs_via_client(
    dest: Path,
    logical_path: str,
    api: ApiClient,
) -> str:
    nl = NodeLogsClient(api)

    def _run():
        return nl.execute(logical_path)

    try:
        out = call_with_retry(_run)
    except KubeClientError as e:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(
            f"# ERROR host_service_logs: {e}\n# Path: {logical_path}\n",
            encoding="utf-8",
        )
        return f"ERR NodeLogsClient {e}"

    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(out, encoding="utf-8")
    return "OK NodeLogsClient"


# Run a shell command inside an etcd pod and save JSON stdout to dest (etcd health / alarms).
def extract_etcd_json_exec(
    dest: Path,
    shell_cmd: str,
    etcd_namespace: str,
) -> str:
    cfg = client.Configuration.get_default_copy()
    api_client = ApiClient(configuration=cfg)
    core = client.CoreV1Api(api_client)
    try:
        pods = core.list_namespaced_pod(namespace=etcd_namespace).items
    except client.ApiException as e:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({"error": f"list pods: {e}"}), encoding="utf-8")
        return f"ERR list pods {e.status}"
    etcd_pod = None
    for p in pods:
        n = (p.metadata.name or "").lower()
        if "etcd" in n and "guard" not in n:
            etcd_pod = p.metadata.name
            break
    if not etcd_pod and pods:
        etcd_pod = pods[0].metadata.name
    if not etcd_pod:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({"error": "no pod in namespace"}), encoding="utf-8")
        return "ERR no pod"
    try:
        out = stream(
            core.connect_get_namespaced_pod_exec,
            etcd_pod,
            etcd_namespace,
            command=["/bin/sh", "-c", shell_cmd],
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
        )
    except client.ApiException as e:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps({"error": str(e)}), encoding="utf-8")
        return f"ERR {e.status}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(out, bytes):
        out = out.decode("utf-8", errors="replace")
    dest.write_text(out, encoding="utf-8")
    return "OK"


# Write a small placeholder file for paths that only exist on gathered nodes, not via API.
def skip_host_only(dest: Path, logical: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        f"# SKIPPED: not available via Kubernetes API\n# Path: {logical}\n",
        encoding="utf-8",
    )


# Write a YAML file explaining why extraction failed (for humans and tooling).
def _write_error_stub(dest: Path, logical_path: str, message: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    stub = {
        "error": "extraction failed",
        "path": logical_path,
        "detail": message,
        "hints": [
            "Check plural name matches kubectl api-resources (URL plural).",
            "Verify RBAC allows list/get for that resource.",
            "cluster-scoped path: cluster-scoped-resources/<api-group>/<plural>.yaml",
            "namespaced path: namespaces/<ns>/<api-group-or-core|apps|batch>/<plural>.yaml",
        ],
    }
    dest.write_text(
        yaml.dump(stub, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )


# Handle one line from file_paths.md: fetch YAML, logs, etcd JSON, or skip with a reason.
def process_path(
    dyn: DynamicClient,
    api: ApiClient,
    rel: str,
    out_root: Path,
    etcd_namespace: str,
) -> list[str]:
    lines: list[str] = []
    p = rel.strip().replace("\\", "/")

    if p.startswith("audit_logs/"):
        skip_host_only(out_root / p, p)
        lines.append(f"SKIP host-only: {p}")
        return lines

    # host_service_logs/<masters|workers>/<name>_service.log — node proxy journal API
    if p.startswith("host_service_logs/"):
        lines.append(
            f"{extract_host_service_logs_via_client(out_root / p, p, api)} → {p}"
        )
        return lines

    # cluster-scoped-resources/<api-group>/<plural>.yaml
    m = re.match(r"^cluster-scoped-resources/([\w.-]+)/([\w-]+)\.yaml$", p)
    if m:
        group, plural = m.group(1), m.group(2)
        try:
            msg = extract_list_with_discovery(
                dyn,
                group,
                plural,
                namespaced=False,
                namespace=None,
                dest=out_root / p,
                logical_path=p,
            )
            lines.append(f"{msg} → {p}")
        except ResourceAbsentOnCluster as e:
            lines.append(f"NOT_PRESENT_ON_CLUSTER {p}: {e}")
            _write_error_stub(out_root / p, p, str(e))
        except RetrievalFailedAfterRetries as e:
            lines.append(f"RETRIEVAL_FAILED {p}: {e}")
            _write_error_stub(out_root / p, p, str(e))
        except Exception as e:  # noqa: BLE001
            lines.append(f"ERR {p}: {e}")
            _write_error_stub(out_root / p, p, str(e))
        return lines

    # namespaces/<ns>/<api-group-or-alias>/<plural>.yaml  (any plural — discovery)
    m = re.match(r"^namespaces/([^/]+)/([\w.-]+)/([\w-]+)\.yaml$", p)
    if m:
        ns, seg, stem = m.group(1), m.group(2), m.group(3)
        group = api_group_from_path_segment(seg)
        plural = stem
        try:
            msg = extract_list_with_discovery(
                dyn,
                group,
                plural,
                namespaced=True,
                namespace=ns,
                dest=out_root / p,
                logical_path=p,
            )
            lines.append(f"{msg} → {p}")
        except ResourceAbsentOnCluster as e:
            lines.append(f"NOT_PRESENT_ON_CLUSTER {p}: {e}")
            _write_error_stub(out_root / p, p, str(e))
        except RetrievalFailedAfterRetries as e:
            lines.append(f"RETRIEVAL_FAILED {p}: {e}")
            _write_error_stub(out_root / p, p, str(e))
        except Exception as e:  # noqa: BLE001
            lines.append(f"ERR {p}: {e}")
            _write_error_stub(out_root / p, p, str(e))
        return lines

    # etcd_info/*.json — EtcdClient for known files; else ETCD_CMD_* + legacy exec
    m = re.match(r"^etcd_info/([\w.-]+\.json)$", p)
    if m:
        fname = m.group(1)
        if fname in ETCD_INFO_COMMANDS:
            lines.append(
                f"{extract_etcd_via_client(out_root / p, p, api, etcd_namespace)} "
                f"EtcdClient ns={etcd_namespace} → {p}"
            )
            return lines
        env_key = f"ETCD_CMD_{re.sub(r'[^A-Z0-9]', '_', fname.upper())}"
        cmd = os.environ.get(env_key)
        if not cmd:
            lines.append(
                f"SKIP {p}: unknown etcd_info file; add to etcd_client.ETCD_INFO_COMMANDS "
                f"or set {env_key} to a shell command for legacy exec"
            )
            return lines
        lines.append(
            f"{extract_etcd_json_exec(out_root / p, cmd, etcd_namespace)} "
            f"stream pod exec ns={etcd_namespace} → {p}"
        )
        return lines

    if re.match(r"^namespaces/([^/]+)/pods/.*logs/current\.log$", p):
        ns = re.match(r"^namespaces/([^/]+)/pods/", p).group(1)
        try:
            sub = extract_pod_logs_dynamic(
                dyn, ns, out_root, previous=False, logical_path=p
            )
        except ResourceAbsentOnCluster as e:
            lines.append(f"NOT_PRESENT_ON_CLUSTER {p}: {e}")
            return lines
        except RetrievalFailedAfterRetries as e:
            lines.append(f"RETRIEVAL_FAILED {p}: {e}")
            return lines
        lines.append(f"OK pod logs → {p} ({len(sub)} files)")
        lines.extend(sub[:30])
        if len(sub) > 30:
            lines.append(f"... +{len(sub) - 30} more")
        return lines

    if re.match(r"^namespaces/([^/]+)/pods/.*logs/previous\.log$", p):
        ns = re.match(r"^namespaces/([^/]+)/pods/", p).group(1)
        try:
            sub = extract_pod_logs_dynamic(
                dyn, ns, out_root, previous=True, logical_path=p
            )
        except ResourceAbsentOnCluster as e:
            lines.append(f"NOT_PRESENT_ON_CLUSTER {p}: {e}")
            return lines
        except RetrievalFailedAfterRetries as e:
            lines.append(f"RETRIEVAL_FAILED {p}: {e}")
            return lines
        lines.append(f"OK pod logs previous → {p} ({len(sub)} files)")
        lines.extend(sub[:30])
        if len(sub) > 30:
            lines.append(f"... +{len(sub) - 30} more")
        return lines

    lines.append(f"SKIP unhandled: {p}")
    return lines


# Parse CLI, connect to cluster, run every path, print report and write _extraction_report.txt.
def main() -> int:
    _script_dir = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(
        description="Dynamic discovery live-cluster extraction → Results/temp2/"
    )
    ap.add_argument("--paths-file", type=Path, default=_script_dir / "file_paths.md")
    ap.add_argument("--output", type=Path, default=_script_dir / "temp2",
                    help="Output dir for extracted files. MCP passes CLUSTER_EXTRACT_DIR/job_id when calling programmatically.")
    ap.add_argument("--kubeconfig", type=str, default=None)
    ap.add_argument(
        "--etcd-namespace",
        default=os.environ.get("ETCD_NAMESPACE", "openshift-etcd"),
        help="Namespace for etcd pod exec (default: openshift-etcd or $ETCD_NAMESPACE)",
    )
    ap.add_argument(
        "--refresh-discovery",
        action="store_true",
        help="Invalidate client discovery cache before run (fixes stale /api discovery)",
    )
    ap.add_argument(
        "--retrieve-retries",
        type=int,
        default=None,
        metavar="N",
        help="Max attempts for retriable LIST/GET failures (default: 3, env MG_RETRIEVE_RETRIES)",
    )
    ap.add_argument(
        "--retrieve-delay",
        type=float,
        default=None,
        metavar="SEC",
        help="Base delay between retries, multiplied by attempt (default: 1.0, env MG_RETRIEVE_DELAY)",
    )
    args = ap.parse_args()

    if args.retrieve_retries is not None:
        os.environ["MG_RETRIEVE_RETRIES"] = str(args.retrieve_retries)
    if args.retrieve_delay is not None:
        os.environ["MG_RETRIEVE_DELAY"] = str(args.retrieve_delay)

    if not args.paths_file.is_file():
        print(f"Missing: {args.paths_file}", file=sys.stderr)
        return 1

    try:
        if args.kubeconfig:
            config.load_kube_config(config_file=args.kubeconfig)
        else:
            config.load_kube_config()
    except config.ConfigException:
        try:
            config.load_incluster_config()
        except config.ConfigException:
            print("No kubeconfig / in-cluster config.", file=sys.stderr)
            return 1

    api = ApiClient()
    dyn = DynamicClient(api)
    if args.refresh_discovery or os.environ.get("MG_REFRESH_DISCOVERY", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        dyn.resources.invalidate_cache()

    paths = parse_paths_from_md(args.paths_file)
    args.output.mkdir(parents=True, exist_ok=True)
    all_lines: list[str] = []
    for rel in paths:
        all_lines.extend(
            process_path(
                dyn,
                api,
                rel,
                args.output,
                etcd_namespace=args.etcd_namespace,
            )
        )

    rep = args.output / "_extraction_report.txt"
    rep.write_text("\n".join(all_lines), encoding="utf-8")
    print("\n".join(all_lines))
    print(f"\nReport: {rep}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
