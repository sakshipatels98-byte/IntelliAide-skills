"""Shared errors for cluster helper clients."""


class KubeClientError(Exception):
    """User-facing error from EtcdClient / NodeLogsClient operations."""
