"""
app/services/configmap_service.py

ConfigMapService — reads ConfigMaps from a cluster.

Listing is a lower privilege than reading content (CONTEXT.md, "ConfigMap
listing" / "ConfigMap content"): a listing says which ConfigMaps exist and what
shape they have, and never carries a value or an annotation.
"""

from __future__ import annotations

import logging

from kubernetes.client import CoreV1Api, V1ConfigMap
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app.core.exceptions import KubeApiException
from app.domain.kubernetes_models import ConfigMapListData, ConfigMapSummary
from app.services.kube_errors import connection_error

_logger = logging.getLogger(__name__)


class ConfigMapService:
    """Lists ConfigMaps."""

    def list_configmaps(
        self,
        cluster: str,
        namespace: str,
        kube: CoreV1Api,
        name_prefixes: list[str] | None = None,
    ) -> ConfigMapListData:
        """List ConfigMaps in *namespace* (``"*"`` = every namespace).

        ``name_prefixes`` is a prefix match, values OR'd; empty/None does not
        filter — the same semantics as the pod listing.

        Raises:
            KubeApiException: On Kubernetes API failure.
        """
        try:
            if namespace == "*":
                cm_list = kube.list_config_map_for_all_namespaces()
            else:
                cm_list = kube.list_namespaced_config_map(namespace)
        except ApiException as exc:
            raise KubeApiException(
                f"Failed to list configmaps in namespace '{namespace}' "
                f"of cluster '{cluster}': {exc.reason}",
                kube_status=exc.status,
            ) from exc
        except Urllib3HTTPError as exc:
            raise connection_error(cluster, exc) from exc

        prefixes = tuple(name_prefixes) if name_prefixes else None
        configmaps = [
            _to_summary(cm)
            for cm in cm_list.items
            if prefixes is None or cm.metadata.name.startswith(prefixes)
        ]

        _logger.info(
            "Listed %d configmap(s) | cluster=%s | namespace=%s",
            len(configmaps), cluster, namespace,
        )
        return ConfigMapListData(cluster=cluster, namespace=namespace, configmaps=configmaps)


def _to_summary(cm: V1ConfigMap) -> ConfigMapSummary:
    keys = sorted({*(cm.data or {}), *(cm.binary_data or {})})
    return ConfigMapSummary(
        name=cm.metadata.name,
        namespace=cm.metadata.namespace,
        keys=keys,
        labels=cm.metadata.labels or {},
        creation_timestamp=cm.metadata.creation_timestamp,
    )
