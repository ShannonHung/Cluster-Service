"""
app/services/namespace_service.py

NamespaceService — reads namespaces from a cluster.

Vocabulary follows CONTEXT.md, "Namespaces". The service never deletes a
namespace: deletion takes everything inside it, irreversibly.
"""

from __future__ import annotations

import logging

from kubernetes.client import CoreV1Api

from app.core.exceptions import NamespaceNotFoundException
from app.domain.kubernetes_models import NamespaceDetailData
from app.services.kube_errors import translate_kube_errors

_logger = logging.getLogger(__name__)

# A stale copy of the manifest as of the last `kubectl apply` — not the current
# state, so it is left out of a read (as for a ConfigMap).
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


class NamespaceService:
    """Reads one namespace."""

    def get_namespace(self, cluster: str, name: str, kube: CoreV1Api) -> NamespaceDetailData:
        """Read one namespace.

        Raises:
            NamespaceNotFoundException: The namespace does not exist.
            KubeApiException: On any other Kubernetes API failure.
        """
        with translate_kube_errors(
            cluster,
            f"read namespace '{name}' of cluster '{cluster}'",
            not_found=lambda: NamespaceNotFoundException(
                f"Namespace '{name}' not found in cluster '{cluster}'.",
            ),
        ):
            ns = kube.read_namespace(name)

        _logger.info("Read namespace | cluster=%s | name=%s", cluster, name)
        annotations = {
            k: v for k, v in (ns.metadata.annotations or {}).items() if k != _LAST_APPLIED
        }
        return NamespaceDetailData(
            cluster=cluster,
            name=ns.metadata.name,
            status=ns.status.phase if ns.status else "",
            labels=ns.metadata.labels or {},
            annotations=annotations,
            creation_timestamp=ns.metadata.creation_timestamp,
            deletion_timestamp=ns.metadata.deletion_timestamp,
        )
