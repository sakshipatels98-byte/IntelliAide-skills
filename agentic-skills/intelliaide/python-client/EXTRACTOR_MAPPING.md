# Extractor → Kubernetes Python client

| Helper in `must-gather-file-extraction.py` | Kubernetes API method(s) | Example path under `temp/` |
|-------------------------------------------|----------------------------|----------------------------|
| `extract_events_yaml` | `CoreV1Api.list_namespaced_event` | `namespaces/<ns>/core/events.yaml` |
| `extract_deployments_yaml` | `AppsV1Api.list_namespaced_deployment` | `namespaces/<ns>/apps/deployments.yaml` |
| `extract_jobs_yaml` | `BatchV1Api.list_namespaced_job` | `namespaces/<ns>/batch/jobs.yaml` |
| `extract_pod_logs` | `CoreV1Api.list_namespaced_pod` + `CoreV1Api.read_namespaced_pod_log` | `namespaces/<ns>/pods/<pod>/<container>/logs/current.log` or `previous.log` |
| `extract_cluster_crd_yaml` | `CustomObjectsApi.list_cluster_custom_object` | `cluster-scoped-resources/<group>/<plural>.yaml` |
| `extract_etcd_json_via_exec` | `stream(CoreV1Api.connect_get_namespaced_pod_exec, …)` | `etcd_info/endpoint_health.json`, `alarm_list.json` |
| `skip_host_only_path` | *(none — not on API)* | `audit_logs/...`, `host_service_logs/...` |

Extend **`CLUSTER_CRD_MAP`** in the script for more `cluster-scoped-resources/...` files.

Default paths file: **`file_paths.md`**.

---

## `live-cluster-extraction.py` (Dynamic client)

Discovery-driven (no hardcoded API versions / kinds for YAML lists):

| Path pattern | Mechanism |
|--------------|-----------|
| `cluster-scoped-resources/<api-group>/<plural>.yaml` | Flatten `dyn.resources` (each yield is a list of `Resource`), then `search(group, name=plural)` if needed to lazy-load that API group; prefer `preferred`; try LIST across versions if needed |
| `namespaces/<ns>/<segment>/<plural>.yaml` | Same, namespaced; `segment` `core` → core API (`group ""`); any other segment = literal API group (e.g. `apps`, `batch`, `config.openshift.io`) |
| `namespaces/<ns>/pods/.../current.log` / `previous.log` | Discover `pods` in core group + `GET .../log` |
| `etcd_info/*.json` (known names) | **`EtcdClient`** (`etcd_client.py`): exec `etcdctl` in running etcd pod |
| `host_service_logs/<masters\|workers>/<unit>_service.log` | **`NodeLogsClient`** (`node_logs_client.py`): `GET /api/v1/nodes/<node>/proxy/logs/journal?unit=&query=` per node |
| `audit_logs/` | Placeholder (no API) |

**Plural** in the filename must match **`kubectl api-resources`** (URL plural), e.g. `clusteroperators`, `storageversionmigrations`.
