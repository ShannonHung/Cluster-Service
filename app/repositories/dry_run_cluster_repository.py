"""
app/repositories/dry_run_cluster_repository.py

A ``ClusterRepository`` implementation used when ``DRY_RUN_MODE=true``.

The real implementations resolve a cluster name by reading a kubeconfig or a
service-account credential file from ``KUBECONFIG_BASE_PATH``. That read happens
*before* the client factory is reached, so stubbing only the factory would still
require credential files on disk — and a dry-run CI instance must hold no
cluster credentials at all.

Every name resolves, so an e2e test never has to know which clusters a
particular deployment happens to have configured. The returned config is
deliberately inert: ``DryRunCoreV1Api`` ignores it and no connection is ever
attempted, but it still satisfies ``KubeClientConfig`` so the layering above is
unchanged.

Design shared with deploy-service; see that repo's docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import logging

from app.domain.kubernetes_models import ClusterInfo, KubeClientConfig
from app.repositories.cluster_repository import ClusterRepository

_logger = logging.getLogger(__name__)

# Deliberately unroutable and obviously fake. `.invalid` is reserved by RFC 2606
# and can never resolve, so a config that escaped into real client construction
# would fail loudly instead of reaching something.
_DRY_RUN_SERVER = "https://dry-run.invalid:6443"

DRY_RUN_CLUSTERS = ("dry-run-cluster", "dry-run-cluster-2")


class DryRunClusterRepository(ClusterRepository):
    """Resolves any cluster name without touching the filesystem."""

    def get_kube_client_config(self, cluster: str) -> KubeClientConfig:
        """Resolve *cluster* to an inert token-auth config.

        Never raises ClusterNotFoundException: an e2e pipeline should not have
        to match whatever cluster names a deployment was configured with. Host
        and node *existence* is still enforced one layer down by
        DryRunCoreV1Api, which 404s an unknown node — so the not-found paths
        that matter to a caller remain reachable.

        ``source="json"`` (token auth) rather than ``"yaml"``: the yaml branch
        of the factory would try to load a file from disk.
        """
        _logger.warning(
            "DRY-RUN | op=cluster.get_kube_client_config | cluster=%s | "
            "no kubeconfig was read",
            cluster,
        )
        return KubeClientConfig(
            cluster_name=cluster,
            source="json",
            server=_DRY_RUN_SERVER,
            token="dry-run-token-not-a-credential",
            ca_data=None,
        )

    def list_clusters(self) -> list[ClusterInfo]:
        """Return a fixed, non-empty listing.

        Non-empty on purpose: an empty list is also a valid real response, so a
        test asserting "the endpoint works" could pass against a repository
        that had silently failed to find anything.
        """
        _logger.warning(
            "DRY-RUN | op=cluster.list_clusters | no filesystem was read"
        )
        return [ClusterInfo(name=name, source="json") for name in DRY_RUN_CLUSTERS]
