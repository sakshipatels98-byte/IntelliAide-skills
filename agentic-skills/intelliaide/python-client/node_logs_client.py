"""Fetch host service logs from cluster nodes via the Kubernetes node proxy API.

Replicates the behavior of `oc adm node-logs --role=<role> -u <service>`,
which hits:
    GET /api/v1/nodes/<node>/proxy/logs/journal?unit=<service>&query=<service>

The gather_service_logs script writes files to:
    host_service_logs/masters/<service>_service.log
    host_service_logs/workers/<service>_service.log
"""

import re

from kubernetes import client as k8s_client

from inspect_kube_adapter.client import KubeClientError

ROLE_LABEL_PREFIX = "node-role.kubernetes.io/"

# Maps role directory names to node-role label values
ROLE_MAP = {
    "masters": "master",
    "workers": "worker",
}


class NodeLogsClient:
    """Fetch systemd service logs from nodes, matching must-gather host_service_logs layout."""

    def __init__(self, api_client: k8s_client.ApiClient):
        self._core_v1 = k8s_client.CoreV1Api(api_client)

    def handles(self, raw_path: str) -> bool:
        stripped = raw_path.strip("/")
        return stripped.startswith("host_service_logs/")

    def execute(self, raw_path: str) -> str:
        stripped = raw_path.strip("/")
        parts = stripped.split("/")

        # host_service_logs/<role_dir>/<service>_service.log
        if len(parts) != 3:
            raise KubeClientError(
                f"Invalid host_service_logs path: {raw_path}. "
                "Expected: host_service_logs/<masters|workers>/<service>_service.log"
            )

        role_dir = parts[1]
        filename = parts[2]

        if role_dir not in ROLE_MAP:
            raise KubeClientError(
                f"Unknown role directory: {role_dir}. "
                f"Expected one of: {', '.join(sorted(ROLE_MAP))}"
            )

        match = re.match(r"^(.+)_service\.log$", filename)
        if not match:
            raise KubeClientError(
                f"Invalid service log filename: {filename}. "
                "Expected format: <service>_service.log"
            )

        service_name = match.group(1)
        role = ROLE_MAP[role_dir]
        return self._get_service_logs(role, service_name)

    def _get_service_logs(self, role: str, service: str) -> str:
        """Fetch journal logs for a systemd unit from all nodes with the given role."""
        label_selector = f"{ROLE_LABEL_PREFIX}{role}"
        nodes = self._core_v1.list_node(label_selector=label_selector)

        if not nodes.items:
            raise KubeClientError(
                f"No nodes found with role '{role}' "
                f"(label: {label_selector})"
            )

        all_logs = []
        for node in nodes.items:
            node_name = node.metadata.name
            try:
                log_text = self._fetch_node_journal(node_name, service)
                all_logs.append(log_text)
            except k8s_client.ApiException as e:
                all_logs.append(
                    f"-- Error fetching {service} logs from {node_name}: {e.reason} --\n"
                )

        return "".join(all_logs)

    def _fetch_node_journal(self, node_name: str, service: str) -> str:
        """Fetch journal logs for a service from a single node.

        Uses the same API path as `oc adm node-logs`:
            GET /api/v1/nodes/{node}/proxy/logs/journal?unit={service}&query={service}
        """
        return self._core_v1.api_client.call_api(
            "/api/v1/nodes/{name}/proxy/logs/journal",
            "GET",
            path_params={"name": node_name},
            query_params=[
                ("unit", service),
                ("query", service),
            ],
            header_params={
                "Accept": "text/plain, */*",
            },
            response_type="str",
            auth_settings=["BearerToken"],
            _return_http_data_only=True,
        )
