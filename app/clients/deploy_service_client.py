"""
app/clients/deploy_service_client.py

Async HTTP client for deploy-service with automatic token management and retry.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.core.exceptions import DeployServiceError, UpstreamServiceException
from app.core.token_manager import TokenManager
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    NodeBastionResolution,
)
from app.domain.pipeline_models import (
    PipelineData,
    RunningPipelinesData,
    TriggerPipelineRequest,
)

_logger = logging.getLogger(__name__)


class DeployServiceClient:
    """Async HTTP client that communicates with deploy-service.

    Uses TokenManager to handle authentication automatically.
    Implements 401 retry logic for robust service-to-service communication.
    """

    def __init__(
        self,
        base_url: str,
        token_manager: TokenManager,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_manager = token_manager
        self._timeout = timeout

    # ── private helpers ───────────────────────────────────────────────────────

    async def _headers(self) -> dict[str, str]:
        token = await self._token_manager.get_token()
        return {"Authorization": f"Bearer {token}"}

    def _raise_for_error(
        self, response: httpx.Response, context: str
    ) -> None:
        """Map a non-2xx response to ``DeployServiceError``."""
        try:
            body: dict[str, Any] = response.json()
        except Exception:
            body = {}

        _logger.error(
            "deploy-service error | context=%s | status=%s | body=%s",
            context,
            response.status_code,
            body,
        )
        raise DeployServiceError(http_status=response.status_code, body=body)

    async def _request_with_retry(
        self, method: str, path: str, context: str, **kwargs
    ) -> dict[str, Any]:
        """Execute request with one-time 401 retry logic."""
        headers = await self._headers()
        kwargs["headers"] = {**kwargs.get("headers", {}), **headers}

        async with httpx.AsyncClient(
            base_url=self._base_url, timeout=self._timeout
        ) as client:
            response = await client.request(method, path, **kwargs)
            
            # If 401, refresh token and retry once
            if response.status_code == 401:
                _logger.warning(f"Received 401 from deploy-service during {context}. Refreshing token and retrying...")
                await self._token_manager.refresh()
                
                # Update headers with new token
                headers = await self._headers()
                kwargs["headers"] = {**kwargs.get("headers", {}), **headers}
                
                response = await client.request(method, path, **kwargs)

        if response.is_error:
            self._raise_for_error(response, context)
            
        return response.json()

    # ── public API ────────────────────────────────────────────────────────────

    async def trigger_pipeline(
        self,
        action: str,
        ref_name: str,
        variables: list[PipelineVariable],
    ) -> PipelineData:
        """Trigger a new pipeline via deploy-service."""
        body = TriggerPipelineRequest(variables=variables)
        raw = await self._request_with_retry(
            "POST",
            "/api/v1/deploy/stage",
            context="trigger_pipeline",
            params={"action": action, "ref_name": ref_name},
            json=body.model_dump(),
        )
        return PipelineData(**raw["data"])

    async def check_running(
        self,
        action: str,
        ref_name: str,
        variables: list[PipelineVariable],
    ) -> RunningPipelinesData:
        """Check for duplicate running pipelines via deploy-service."""
        body = TriggerPipelineRequest(variables=variables)
        raw = await self._request_with_retry(
            "POST",
            "/api/v1/deploy/stage/check-running",
            context="check_running",
            params={"action": action, "ref_name": ref_name},
            json=body.model_dump(),
        )
        return RunningPipelinesData(**raw["data"])

    async def get_pipeline(self, pipeline_id: int) -> PipelineData:
        """Retrieve current pipeline state via deploy-service."""
        raw = await self._request_with_retry(
            "GET",
            f"/api/v1/deploy/stage/{pipeline_id}",
            context="get_pipeline",
        )
        return PipelineData(**raw["data"])

    async def cancel_pipeline(self, pipeline_id: int) -> PipelineData:
        """Cancel a running pipeline via deploy-service."""
        raw = await self._request_with_retry(
            "POST",
            f"/api/v1/deploy/stage/{pipeline_id}/cancel",
            context="cancel_pipeline",
        )
        return PipelineData(**raw["data"])

    async def retry_pipeline(self, pipeline_id: int) -> PipelineData:
        """Retry a failed / cancelled pipeline via deploy-service."""
        raw = await self._request_with_retry(
            "POST",
            f"/api/v1/deploy/stage/{pipeline_id}/retry",
            context="retry_pipeline",
        )
        return PipelineData(**raw["data"])

    # ── inventory proxy ───────────────────────────────────────────────────────

    async def get_node(self, node_name: str) -> ClusterNodeInfo:
        """Look up cluster node info by node name."""
        raw = await self._request_with_retry(
            "GET",
            f"/api/v1/inventory/nodes/{node_name}",
            context="inventory.get_node",
        )
        return ClusterNodeInfo(**raw["data"])

    async def list_mappings(self, type_name: str) -> list[BastionMapping]:
        """List bastion-cluster mappings for a bastion type."""
        raw = await self._request_with_retry(
            "GET",
            "/api/v1/inventory/mappings",
            context="inventory.list_mappings",
            params={"type": type_name},
        )
        return [BastionMapping(**item) for item in raw["data"]]

    async def resolve_node_bastion(
        self, node_name: str, bastion_type: str | None = None
    ) -> NodeBastionResolution:
        """Resolve a node name to its bastion runner."""
        params: dict[str, str] = {}
        if bastion_type is not None:
            params["bastion_type"] = bastion_type
        raw = await self._request_with_retry(
            "GET",
            f"/api/v1/inventory/nodes/{node_name}/bastion-resolution",
            context="inventory.resolve_node_bastion",
            params=params,
        )
        return NodeBastionResolution(**raw["data"])

    async def resolve_cluster_bastion(
        self, cluster_name: str
    ) -> ClusterBastionResolution:
        """Resolve a cluster name to its bastion runner."""
        raw = await self._request_with_retry(
            "GET",
            "/api/v1/inventory/cluster/bastion-resolution",
            context="inventory.resolve_cluster_bastion",
            params={"cluster_name": cluster_name},
        )
        return ClusterBastionResolution(**raw["data"])

