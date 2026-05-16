"""Gather etcd cluster info from a live cluster by exec-ing into etcd pods."""

import os

from kubernetes import client as k8s_client
from kubernetes.stream import stream

from inspect_kube_adapter.client import KubeClientError

DEFAULT_ETCD_NAMESPACE = "openshift-etcd"
ETCDCTL_CONTAINER = "etcdctl"

ETCD_INFO_COMMANDS = {
    "member_list.json": "etcdctl member list -w json",
    "endpoint_status.json": "etcdctl endpoint status -w json",
    "endpoint_health.json": "etcdctl endpoint health -w json",
    "alarm_list.json": "etcdctl alarm list -w json",
    "object_count.json": (
        'etcdctl get / --prefix --keys-only'
        ' | grep -oE "^/[a-z|.]+/[a-z|.|8]*"'
        ' | sort | uniq -c | sort -rn'
        ' | while read KEY; do'
        ' printf "$KEY\\t"'
        ' && etcdctl get ${KEY##* } --prefix --print-value-only'
        ' | wc -c | numfmt --to=iec ;'
        ' done | sort -k3 -hr'
        ' | column -J -n "etcd_resources"'
        ' -N "object_count,object_name,object_size"'
    ),
}


class EtcdClient:
    """Exec into etcd pods to gather etcd cluster information."""

    def __init__(
        self,
        api_client: k8s_client.ApiClient,
        namespace: str | None = None,
    ):
        self._core_v1 = k8s_client.CoreV1Api(api_client)
        self._namespace = (
            namespace
            or os.environ.get("ETCD_NAMESPACE")
            or DEFAULT_ETCD_NAMESPACE
        )
        self._etcd_pod: str | None = None
        self._etcdctl_endpoints: str | None = None

    def handles(self, raw_path: str) -> bool:
        stripped = raw_path.strip("/")
        return stripped.startswith("etcd_info/")

    def execute(self, raw_path: str) -> str:
        stripped = raw_path.strip("/")
        # etcd_info/<filename>
        parts = stripped.split("/", 1)
        if len(parts) != 2:
            raise KubeClientError(f"Invalid etcd_info path: {raw_path}")

        filename = parts[1]
        if filename not in ETCD_INFO_COMMANDS:
            raise KubeClientError(
                f"Unknown etcd_info file: {filename}. "
                f"Valid files: {', '.join(sorted(ETCD_INFO_COMMANDS))}"
            )

        self._ensure_etcd_pod()
        cmd = ETCD_INFO_COMMANDS[filename]
        return self._exec_etcdctl(cmd)

    def _ensure_etcd_pod(self):
        if self._etcd_pod is not None:
            return

        pods = self._core_v1.list_namespaced_pod(
            namespace=self._namespace,
            label_selector="app=etcd",
        )
        running_pod = None
        for pod in pods.items:
            if pod.status.phase == "Running":
                running_pod = pod.metadata.name
                break

        if not running_pod:
            raise KubeClientError(
                f"No running etcd pods found in namespace {self._namespace!r}"
            )

        self._etcd_pod = running_pod

        # Discover endpoints (same as the gather_etcd script)
        member_output = self._exec_in_pod(
            "etcdctl member list", container=ETCDCTL_CONTAINER
        )
        endpoints = []
        for line in member_output.strip().splitlines():
            fields = [f.strip() for f in line.split(",")]
            if len(fields) >= 5:
                endpoints.append(fields[4])
        self._etcdctl_endpoints = ",".join(endpoints)

    def _exec_etcdctl(self, command: str) -> str:
        full_cmd = f'ETCDCTL_ENDPOINTS={self._etcdctl_endpoints} sh -c "{command}"'
        return self._exec_in_pod(full_cmd, container=ETCDCTL_CONTAINER)

    def _exec_in_pod(self, command: str, container: str) -> str:
        try:
            result = stream(
                self._core_v1.connect_get_namespaced_pod_exec,
                self._etcd_pod,
                self._namespace,
                container=container,
                command=["/bin/sh", "-c", command],
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
            )
            return result
        except k8s_client.ApiException as e:
            raise KubeClientError(
                f"Failed to exec in etcd pod '{self._etcd_pod}': {e.reason}"
            ) from e
