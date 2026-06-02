"""
app/services/pipeline_service.py

Orchestrates pipeline operations by delegating to DeployServiceClient.

This layer exists to:
  - Keep the router thin (no HTTP or business logic in route handlers).
  - Allow easy unit testing — inject a mock client instead of DeployServiceClient.
  - Provide a stable interface for future refactoring (e.g. adding caching,
    retries, or audit logging without touching the router).

All exceptions from the client (UpstreamServiceException) propagate upward
unchanged; the global exception handler in main.py converts them to HTTP 502.
"""

from __future__ import annotations

import logging

from app.clients.deploy_service_client import DeployServiceClient
from app.domain.pipeline_models import (
    PipelineData,
    PipelineVariable,
    RunningPipelinesData,
)

_logger = logging.getLogger(__name__)


class PipelineService:
    """Thin orchestration layer over DeployServiceClient.

    Constructor injection makes the client replaceable in tests:
        svc = PipelineService(client=mock_client)
    """

    def __init__(self, client: DeployServiceClient) -> None:
        self._client = client

    # ── Public API ────────────────────────────────────────────────────────────

    async def trigger_pipeline(
        self,
        action: str,
        ref: str,
        extra_variables: list[PipelineVariable],
    ) -> PipelineData:
        """Trigger a new GitLab pipeline through deploy-service."""
        _logger.info(
            "Triggering pipeline | action=%s | ref=%s | extra_vars=%s",
            action, ref, {v.key: v.value for v in extra_variables},
        )
        return await self._client.trigger_pipeline(
            action=action,
            ref_name=ref,
            variables=extra_variables,
        )

    async def check_running(
        self,
        action: str,
        ref: str,
        extra_variables: list[PipelineVariable],
    ) -> RunningPipelinesData:
        """Return active pipelines matching the given action and ref."""
        return await self._client.check_running(
            action=action,
            ref_name=ref,
            variables=extra_variables,
        )

    async def get_pipeline(self, pipeline_id: int) -> PipelineData:
        """Return the current state of a pipeline."""
        return await self._client.get_pipeline(pipeline_id)

    async def cancel_pipeline(self, pipeline_id: int) -> PipelineData:
        """Cancel a running pipeline."""
        _logger.info("Cancelling pipeline | id=%s", pipeline_id)
        return await self._client.cancel_pipeline(pipeline_id)

    async def retry_pipeline(self, pipeline_id: int) -> PipelineData:
        """Retry a failed or cancelled pipeline."""
        _logger.info("Retrying pipeline | id=%s", pipeline_id)
        return await self._client.retry_pipeline(pipeline_id)

