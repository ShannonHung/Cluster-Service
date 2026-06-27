"""
app/services/inventory_service.py

Thin orchestration over DeployServiceClient for the inventory proxy.

Mirrors PipelineService: the router stays HTTP-only and the client is easy to
mock in tests. The only added logic is translating an upstream 404 into
cluster-service's NotFoundException so callers see a 404 (not the generic 502
that DeployServiceError otherwise yields).
"""

from __future__ import annotations

import logging

from app.clients.deploy_service_client import DeployServiceClient
from app.core.exceptions import DeployServiceError, ErrorCode, NotFoundException
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    NodeBastionResolution,
)

_logger = logging.getLogger(__name__)


class _InventoryNotFound(NotFoundException):
    """NotFoundException specialised with the inventory error code."""

    error_code = ErrorCode.INVENTORY_NOT_FOUND


class InventoryProxyService:
    """Thin proxy over DeployServiceClient's inventory methods."""

    def __init__(self, client: DeployServiceClient) -> None:
        self._client = client

    async def get_node(self, node_name: str) -> ClusterNodeInfo:
        try:
            return await self._client.get_node(node_name)
        except DeployServiceError as exc:
            raise self._maybe_not_found(exc, f"node '{node_name}' not found")

    async def list_mappings(self, type_name: str) -> list[BastionMapping]:
        try:
            return await self._client.list_mappings(type_name)
        except DeployServiceError as exc:
            raise self._maybe_not_found(
                exc, f"no bastion mappings for type '{type_name}'"
            )

    async def resolve_node_bastion(
        self, node_name: str, bastion_type: str | None = None
    ) -> NodeBastionResolution:
        try:
            return await self._client.resolve_node_bastion(
                node_name, bastion_type=bastion_type
            )
        except DeployServiceError as exc:
            raise self._maybe_not_found(
                exc, f"could not resolve bastion for node '{node_name}'"
            )

    async def resolve_cluster_bastion(
        self, cluster_name: str
    ) -> ClusterBastionResolution:
        try:
            return await self._client.resolve_cluster_bastion(cluster_name)
        except DeployServiceError as exc:
            raise self._maybe_not_found(
                exc, f"could not resolve bastion for cluster '{cluster_name}'"
            )

    @staticmethod
    def _maybe_not_found(exc: DeployServiceError, message: str) -> Exception:
        """Return a 404 NotFoundException for upstream 404s; else the original error."""
        if exc.upstream_status == 404:
            return _InventoryNotFound(message)
        return exc
