"""
app/services/kube_client.py

KubeClientFactory — creates isolated Kubernetes API client objects.

Accepts a ``KubeClientConfig`` (produced by any ClusterRepository impl)
and builds an ``ApiClient`` using the appropriate auth mechanism:
  - source="yaml" → load from kubeconfig file
  - source="json" | "api" → token + CA data (service-account style)

Each call creates a *fresh* ApiClient + Configuration, preventing
cross-cluster state pollution in concurrent requests.
"""

from __future__ import annotations

import base64
import logging
import ssl
import tempfile
from pathlib import Path

from kubernetes import client, config as kube_config
from kubernetes.client import ApiClient, CoreV1Api, Configuration

from app.core.config import get_settings
from app.core.exceptions import KubeApiException
from app.domain.kubernetes_models import KubeClientConfig
from app.services.dry_run_kube_client import DryRunCoreV1Api

_logger = logging.getLogger(__name__)


class KubeClientFactory:
    """Produces scope-isolated Kubernetes API clients from a KubeClientConfig.

    Usage::

        factory = KubeClientFactory()
        core = factory.get_core_v1(kube_client_config)
        nodes = core.list_node()
    """

    # ── Public helpers ────────────────────────────────────────────────────────

    def get_core_v1(self, cfg: KubeClientConfig) -> CoreV1Api:
        """Return a CoreV1Api client for the cluster described by *cfg*.

        Raises:
            KubeApiException: If the config cannot be loaded.

        In dry-run a fake, CoreV1Api-shaped object is returned instead and no
        connection is opened. NodeService is handed this exactly as it would be
        handed the real client, so all of its business logic — the uncordon
        readiness gate, drain's refuse-before-evict check, the batch failure
        layering — still runs. A fresh instance per call preserves the
        isolation guarantee documented above. See
        app/services/dry_run_kube_client.py.
        """
        if get_settings().DRY_RUN_MODE:
            _logger.warning(
                "DRY-RUN | op=kube.get_core_v1 | cluster=%s | "
                "no Kubernetes client was created",
                cfg.cluster_name,
            )
            # Keyed by cluster so two dry-run clusters do not share state.
            return DryRunCoreV1Api(cfg.cluster_name)  # type: ignore[return-value]

        return CoreV1Api(api_client=self._make_api_client(cfg))

    def get_api_client(self, cfg: KubeClientConfig) -> ApiClient:
        """Return a raw ApiClient (useful for drain helpers that need one directly)."""
        return self._make_api_client(cfg)

    # ── Private ───────────────────────────────────────────────────────────────

    def _make_api_client(self, cfg: KubeClientConfig) -> ApiClient:
        """Dispatch to the right auth strategy based on cfg.source."""
        if cfg.source == "yaml":
            return self._from_yaml(cfg)
        return self._from_token(cfg)

    def _from_yaml(self, cfg: KubeClientConfig) -> ApiClient:
        """Build ApiClient from a kubeconfig YAML file."""
        if not cfg.kubeconfig_path:
            raise KubeApiException(
                f"KubeClientConfig for '{cfg.cluster_name}' has source='yaml' "
                "but no kubeconfig_path.",
            )
        k8s_cfg = Configuration()
        try:
            kube_config.load_kube_config(
                config_file=str(cfg.kubeconfig_path),
                client_configuration=k8s_cfg,
            )
        except Exception as exc:
            _logger.error(
                "Failed to load YAML kubeconfig | cluster=%s | path=%s | error=%s",
                cfg.cluster_name,
                cfg.kubeconfig_path,
                exc,
            )
            raise KubeApiException(
                f"Failed to load kubeconfig for '{cfg.cluster_name}': {exc}",
            ) from exc

        _logger.debug(
            "Loaded YAML kubeconfig | cluster=%s | host=%s",
            cfg.cluster_name,
            k8s_cfg.host,
        )
        return ApiClient(configuration=k8s_cfg)

    def _from_token(self, cfg: KubeClientConfig) -> ApiClient:
        """Build ApiClient from server URL + bearer token + CA data."""
        if not cfg.server or not cfg.token:
            raise KubeApiException(
                f"KubeClientConfig for '{cfg.cluster_name}' has source='{cfg.source}' "
                "but is missing 'server' or 'token'.",
            )

        k8s_cfg = Configuration()
        k8s_cfg.host = cfg.server
        k8s_cfg.api_key = {"authorization": f"Bearer {cfg.token}"}

        if cfg.ca_data:
            # Write CA to a temp file — kubernetes client requires a file path.
            ca_bytes = base64.b64decode(cfg.ca_data)
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
            tmp.write(ca_bytes)
            tmp.flush()
            tmp.close()
            k8s_cfg.ssl_ca_cert = tmp.name
        else:
            # No CA provided — disable verification (use only in dev/test).
            _logger.warning(
                "No CA data for cluster '%s' — TLS verification disabled.",
                cfg.cluster_name,
            )
            k8s_cfg.verify_ssl = False

        _logger.debug(
            "Loaded token auth config | cluster=%s | server=%s",
            cfg.cluster_name,
            cfg.server,
        )
        return ApiClient(configuration=k8s_cfg)
