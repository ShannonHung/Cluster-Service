"""
app/services/kube_client.py

KubeClientFactory — creates isolated Kubernetes API client objects.

Accepts a ``KubeClientConfig`` (produced by any ClusterRepository impl)
and builds an ``ApiClient`` using the appropriate auth mechanism:
  - source="yaml" → load from kubeconfig file
  - source="json" | "api" → token + CA data (service-account style)

Each call creates a *fresh* ApiClient + Configuration, preventing
cross-cluster state pollution in concurrent requests — so each one is released
when its request is done (``KubeClientFactory.release``, called by
``call_kube``).
"""

from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import logging
import os
import ssl
import tempfile
import threading
from pathlib import Path
from typing import Callable, TypeVar

from kubernetes import client, config as kube_config
from kubernetes.client import ApiClient, CoreV1Api, Configuration

from app.core.config import get_settings
from app.core.exceptions import KubeApiException
from app.domain.kubernetes_models import KubeClientConfig
from app.repositories.cluster_repository import ClusterRepository
from app.services.dry_run_kube_client import DryRunCoreV1Api

_logger = logging.getLogger(__name__)

T = TypeVar("T")

# CA file per distinct CA, keyed by content hash. The SDK accepts a CA only as
# a file path; writing one per request and never deleting it grew the temp
# directory without bound. A cluster's CA is the same on every request, so it
# is written once and reused for the life of the process (as the SDK itself
# does for kubeconfig-embedded CAs), and removed at exit.
_CA_FILES: dict[str, str] = {}
_CA_FILES_LOCK = threading.Lock()


def _ca_file(ca_bytes: bytes) -> str:
    key = hashlib.sha256(ca_bytes).hexdigest()
    with _CA_FILES_LOCK:
        path = _CA_FILES.get(key)
        if path is None or not os.path.exists(path):  # a tmp cleaner may have removed it
            with tempfile.NamedTemporaryFile(delete=False, suffix=".crt") as tmp:
                tmp.write(ca_bytes)
            path = _CA_FILES[key] = tmp.name
        return path


@atexit.register
def _remove_ca_files() -> None:
    for path in _CA_FILES.values():
        try:
            os.remove(path)
        except OSError:
            pass


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

    @staticmethod
    def release(kube: CoreV1Api) -> None:
        """Release what a client from ``get_core_v1`` holds.

        ``ApiClient.close()`` alone is not enough: it only shuts the thread
        pool used by ``async_req`` calls, which synchronous calls never create.
        The sockets to the API server live in the REST client's urllib3 pool
        manager, which has to be cleared. Anything without an ``api_client``
        (the dry-run fake) holds no connections and is left alone.
        """
        api_client = getattr(kube, "api_client", None)
        if api_client is None:
            return
        api_client.rest_client.pool_manager.clear()
        api_client.close()

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
            # The SDK requires a file path; one file per distinct CA, reused.
            k8s_cfg.ssl_ca_cert = _ca_file(base64.b64decode(cfg.ca_data))
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


async def call_kube(
    repo: ClusterRepository, cluster: str, op: Callable[[CoreV1Api], T]
) -> T:
    """Resolve *cluster*, build a client for it, and run ``op(client)`` — all
    in a worker thread.

    All three block: resolving credentials reads a kubeconfig and may run an
    exec credential plugin (EKS / GKE); building the client can write a CA
    temp file; every SDK call blocks on a urllib3 socket. On the event loop any
    one of them stalls every other request, health checks included. Routes
    call this rather than threading the pieces themselves, so none of the
    three can be left behind (CLAUDE.md, "Never call a Kubernetes service
    inline from a route").

    A fresh client per call, as ``KubeClientFactory`` requires — and released
    when the call is done, whether it returned or raised, so connections to
    the API server do not pile up until garbage collection.
    """

    def _run() -> T:
        cfg = repo.get_kube_client_config(cluster)
        factory = KubeClientFactory()
        kube = factory.get_core_v1(cfg)
        try:
            return op(kube)
        finally:
            factory.release(kube)

    return await asyncio.to_thread(_run)
