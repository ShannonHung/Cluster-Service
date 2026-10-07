"""
app/services/configmap_service.py

ConfigMapService — reads ConfigMaps from a cluster.

Listing is a lower privilege than reading content (CONTEXT.md, "ConfigMap
listing" / "ConfigMap content"): a listing says which ConfigMaps exist and what
shape they have, and never carries a value or an annotation.
"""

from __future__ import annotations

import logging

from kubernetes.client import CoreV1Api, V1ConfigMap, V1ConfigMapList

from app.core.exceptions import ConfigMapNotFoundException
from app.domain.kubernetes_models import (
    ConfigMapDetailData,
    ConfigMapListData,
    ConfigMapSummary,
)
from app.services.kube_errors import translate_kube_errors

_logger = logging.getLogger(__name__)

# ConfigMaps per request to the API server. Kubernetes cannot list ConfigMaps
# without their values (up to 1 MiB each), so a listing that only needs key
# names still receives every value; paging bounds how many are held at once.
PAGE_SIZE = 500

# A copy of the values as of the last `kubectl apply`. Not content: after a
# `kubectl edit` it disagrees with data (CONTEXT.md, "ConfigMap content").
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


class ConfigMapService:
    """Lists ConfigMaps and reads one ConfigMap's content."""

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
        prefixes = tuple(name_prefixes) if name_prefixes else None
        configmaps: list[ConfigMapSummary] = []
        token: str | None = None
        while True:
            page = self._fetch_page(cluster, namespace, kube, token)
            configmaps.extend(
                _to_summary(cm)
                for cm in page.items
                if prefixes is None or cm.metadata.name.startswith(prefixes)
            )
            token = page.metadata._continue if page.metadata else None
            if not token:  # the API server sends "" (or nothing) on the last page
                break

        _logger.info(
            "Listed %d configmap(s) | cluster=%s | namespace=%s",
            len(configmaps), cluster, namespace,
        )
        return ConfigMapListData(cluster=cluster, namespace=namespace, configmaps=configmaps)

    def get_configmap(
        self, cluster: str, namespace: str, name: str, kube: CoreV1Api
    ) -> ConfigMapDetailData:
        """Read one ConfigMap's values.

        Raises:
            ConfigMapNotFoundException: The ConfigMap — or its namespace — does not exist.
            KubeApiException: On any other Kubernetes API failure.
        """
        with translate_kube_errors(
            cluster,
            f"read configmap '{namespace}/{name}' of cluster '{cluster}'",
            not_found=lambda: ConfigMapNotFoundException(
                f"ConfigMap '{name}' not found in namespace '{namespace}' "
                f"of cluster '{cluster}' (the namespace may not exist either).",
            ),
        ):
            cm = kube.read_namespaced_config_map(name, namespace)

        _logger.info(
            "Read configmap | cluster=%s | namespace=%s | name=%s",
            cluster, namespace, name,
        )
        annotations = {
            k: v for k, v in (cm.metadata.annotations or {}).items() if k != _LAST_APPLIED
        }
        return ConfigMapDetailData(
            cluster=cluster,
            name=cm.metadata.name,
            namespace=cm.metadata.namespace,
            labels=cm.metadata.labels or {},
            annotations=annotations,
            creation_timestamp=cm.metadata.creation_timestamp,
            data=cm.data or {},
            binary_data=cm.binary_data or {},
        )

    @staticmethod
    def _fetch_page(
        cluster: str, namespace: str, kube: CoreV1Api, token: str | None
    ) -> V1ConfigMapList:
        with translate_kube_errors(
            cluster, f"list configmaps in namespace '{namespace}' of cluster '{cluster}'"
        ):
            if namespace == "*":
                return kube.list_config_map_for_all_namespaces(
                    limit=PAGE_SIZE, _continue=token
                )
            return kube.list_namespaced_config_map(
                namespace, limit=PAGE_SIZE, _continue=token
            )


def _to_summary(cm: V1ConfigMap) -> ConfigMapSummary:
    keys = sorted({*(cm.data or {}), *(cm.binary_data or {})})
    return ConfigMapSummary(
        name=cm.metadata.name,
        namespace=cm.metadata.namespace,
        keys=keys,
        labels=cm.metadata.labels or {},
        creation_timestamp=cm.metadata.creation_timestamp,
    )
