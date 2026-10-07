"""
app/api/v1/deploy.py

Pipeline deployment endpoints — proxied to deploy-service (v1).
All endpoints require the ``deploy_api`` scope.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.clients.deploy_service_client import DeployServiceClient
from app.clients.dry_run_deploy_service_client import DryRunDeployServiceClient
from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.core.token_manager import DeployServiceTokenManager
from app.domain.models import ApiResponse, User
from app.domain.pipeline_models import (
    PipelineData,
    PipelineVariable,
    RunningPipelinesData,
    TriggerPipelineRequest,
)
from app.services.pipeline_service import PipelineService

_logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/deploy",
    tags=["deploy"],
)

# Shared Token Manager instance (module-level singleton for this service)
_deploy_token_manager = None

def get_deploy_token_manager() -> DeployServiceTokenManager:
    global _deploy_token_manager
    if _deploy_token_manager is None:
        settings = get_settings()
        _deploy_token_manager = DeployServiceTokenManager(
            base_url=settings.DEPLOY_SERVICE_URL,
            username=settings.DEPLOY_SERVICE_USERNAME,
            password=settings.DEPLOY_SERVICE_PASSWORD,
            initial_token=settings.DEPLOY_SERVICE_TOKEN
        )
    return _deploy_token_manager

def _get_pipeline_service() -> PipelineService:
    """Build PipelineService backed by a live DeployServiceClient.

    In dry-run the client is swapped for an in-memory stand-in and the token
    manager singleton is never touched, so no token is fetched and no upstream
    is needed. PipelineService itself is unchanged. See
    app/clients/dry_run_deploy_service_client.py.
    """
    settings = get_settings()
    if settings.DRY_RUN_MODE:
        return PipelineService(DryRunDeployServiceClient())  # type: ignore[arg-type]
    client = DeployServiceClient(
        base_url=settings.DEPLOY_SERVICE_URL,
        token_manager=get_deploy_token_manager(),
    )
    return PipelineService(client)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


# ── POST /api/v1/deploy ───────────────────────────────────────────────────────

@router.post(
    "",
    response_model=ApiResponse[PipelineData],
    summary="Trigger a GitLab pipeline",
    description=(
        "Proxies a pipeline trigger request to deploy-service. "
        "The `action` query param is forwarded as the `EXECUTION` pipeline variable. "
        "Body variables are merged in. Raises 409 if an identical pipeline is already running."
    ),
)
async def trigger_pipeline(
    request: Request,
    action: str = Query(..., description="Pipeline EXECUTION variable value (e.g. test-deploy)"),
    ref_name: str = Query(default="main", description="Git branch or tag to run pipeline on"),
    body: TriggerPipelineRequest = TriggerPipelineRequest(),
    svc: PipelineService = Depends(_get_pipeline_service),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[PipelineData]:
    data = await svc.trigger_pipeline(
        action=action,
        ref=ref_name,
        extra_variables=body.variables,
    )
    return ApiResponse(data=data, request_id=_request_id(request))


# ── POST /api/v1/deploy/check-running ───────────────────────────────────

@router.post(
    "/check-running",
    response_model=ApiResponse[RunningPipelinesData],
    summary="Check for duplicate running pipelines",
    description=(
        "Returns all active pipelines on *ref_name* whose variables exactly match "
        "*action* + body variables. Use this before triggering to preview what would be blocked."
    ),
)
async def check_running(
    request: Request,
    action: str = Query(..., description="EXECUTION variable value to match"),
    ref_name: str = Query(default="main", description="Branch or tag to filter on"),
    body: TriggerPipelineRequest = TriggerPipelineRequest(),
    svc: PipelineService = Depends(_get_pipeline_service),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[RunningPipelinesData]:
    data = await svc.check_running(
        action=action,
        ref=ref_name,
        extra_variables=body.variables,
    )
    return ApiResponse(data=data, request_id=_request_id(request))


# ── GET /api/v1/deploy/{pipeline_id} ────────────────────────────────────

@router.get(
    "/{pipeline_id}",
    response_model=ApiResponse[PipelineData],
    summary="Get pipeline status",
    description="Returns the current state of a pipeline from deploy-service.",
)
async def get_pipeline(
    request: Request,
    pipeline_id: int,
    svc: PipelineService = Depends(_get_pipeline_service),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[PipelineData]:
    data = await svc.get_pipeline(pipeline_id)
    return ApiResponse(data=data, request_id=_request_id(request))


# ── POST /api/v1/deploy/{pipeline_id}/cancel ────────────────────────────

@router.post(
    "/{pipeline_id}/cancel",
    response_model=ApiResponse[PipelineData],
    summary="Cancel a pipeline",
    description="Cancels a running pipeline via deploy-service and returns its updated status.",
)
async def cancel_pipeline(
    request: Request,
    pipeline_id: int,
    svc: PipelineService = Depends(_get_pipeline_service),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[PipelineData]:
    data = await svc.cancel_pipeline(pipeline_id)
    return ApiResponse(data=data, request_id=_request_id(request))


# ── POST /api/v1/deploy/{pipeline_id}/retry ─────────────────────────────

@router.post(
    "/{pipeline_id}/retry",
    response_model=ApiResponse[PipelineData],
    summary="Retry a pipeline",
    description="Retries a failed or cancelled pipeline via deploy-service and returns the new state.",
)
async def retry_pipeline(
    request: Request,
    pipeline_id: int,
    svc: PipelineService = Depends(_get_pipeline_service),
    current_user: Annotated[User, Depends(get_current_user(["deploy_api"]))] = None,
) -> ApiResponse[PipelineData]:
    data = await svc.retry_pipeline(pipeline_id)
    return ApiResponse(data=data, request_id=_request_id(request))


