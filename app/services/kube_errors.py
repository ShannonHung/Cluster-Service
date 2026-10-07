"""
app/services/kube_errors.py

Kubernetes error translation shared by every service that talks to a cluster.
"""

from __future__ import annotations

from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app.core.exceptions import KubeApiException


def connection_error(cluster: str, exc: Urllib3HTTPError) -> KubeApiException:
    """Convert a urllib3 network error to a KubeApiException(503).

    Tagged ``cluster_level`` so batch operations can tell an unreachable
    cluster apart from a per-node failure and propagate it instead of
    reporting it once per node.
    """
    err = KubeApiException(
        f"Cannot reach cluster '{cluster}': {exc}",
        kube_status=503,
    )
    err.cluster_level = True
    return err
