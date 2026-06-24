"""
app/api/v1/command.py

SSH command-execution endpoints — proxied to deploy-service (v1).
All endpoints require the ``command_api`` scope, except the unauthed HTML
log-viewer shell (whose polled /trace/ui carries its own token).
"""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.api.v1.deploy import get_deploy_token_manager
from app.clients.command_service_client import CommandServiceClient
from app.core.config import get_settings
from app.core.dependencies import (
    get_current_user,
    get_current_user_cookie_or_header,
)
from app.core.log_viewer_template import LOG_VIEWER_HTML
from app.core.token_manager import DeployServiceTokenManager
from app.domain.command_models import (
    CommandExecutionRequest,
    CommandExecutionResponse,
    CommandTraceResponse,
    CommandWhitelistConfig,
    UserCommandWhitelist,
)
from app.domain.models import ApiResponse, User
from app.services.command_service import CommandService

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/command", tags=["command"])


def _get_command_service(
    token_manager: DeployServiceTokenManager = Depends(get_deploy_token_manager),
) -> CommandService:
    """Build a CommandService backed by a live CommandServiceClient.

    Reuses the shared deploy-service token manager singleton (same upstream
    identity as the pipeline proxy)."""
    settings = get_settings()
    client = CommandServiceClient(
        base_url=settings.DEPLOY_SERVICE_URL,
        token_manager=token_manager,
    )
    return CommandService(client)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


@router.get(
    "/info",
    response_model=ApiResponse[UserCommandWhitelist],
    summary="List available commands",
)
async def get_all_commands_info(
    request: Request,
    svc: CommandService = Depends(_get_command_service),
    current_user: Annotated[User, Depends(get_current_user(["command_api"]))] = None,
) -> ApiResponse[UserCommandWhitelist]:
    data = await svc.get_all_commands()
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/{command_name}/info",
    response_model=ApiResponse[CommandWhitelistConfig],
    summary="Get a specific command's definition",
)
async def get_command_info(
    command_name: str,
    request: Request,
    svc: CommandService = Depends(_get_command_service),
    current_user: Annotated[User, Depends(get_current_user(["command_api"]))] = None,
) -> ApiResponse[CommandWhitelistConfig]:
    data = await svc.get_command(command_name)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.post(
    "/execution",
    response_model=ApiResponse[CommandExecutionResponse],
    summary="Execute a command pipeline",
)
async def execute_command(
    request: Request,
    body: CommandExecutionRequest,
    svc: CommandService = Depends(_get_command_service),
    current_user: Annotated[User, Depends(get_current_user(["command_api"]))] = None,
) -> ApiResponse[CommandExecutionResponse]:
    data = await svc.execute(body)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/execution/{command_id}",
    response_model=ApiResponse[CommandExecutionResponse],
    summary="Poll command execution result",
)
async def get_command_execution_status(
    command_id: str,
    request: Request,
    svc: CommandService = Depends(_get_command_service),
    current_user: Annotated[User, Depends(get_current_user(["command_api"]))] = None,
) -> ApiResponse[CommandExecutionResponse]:
    data = await svc.get_result(command_id)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/execution/{command_id}/trace/ui",
    response_model=ApiResponse[CommandTraceResponse],
    summary="Incremental command log slice for the UI",
)
async def get_command_trace_ui(
    command_id: str,
    request: Request,
    byte_offset: int = Query(0, ge=0),
    line_num: int = Query(1, ge=1),
    svc: CommandService = Depends(_get_command_service),
    current_user: Annotated[
        User, Depends(get_current_user_cookie_or_header(["command_api"]))
    ] = None,
) -> ApiResponse[CommandTraceResponse]:
    data = await svc.get_trace(command_id, byte_offset, line_num)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/execution/{command_id}/view",
    response_class=HTMLResponse,
    summary="View command logs in a browser",
)
async def view_command(command_id: str):
    # Unauthed HTML shell; the /trace/ui it polls carries its own command_api token.
    trace_url = f"/api/v1/command/execution/{command_id}/trace/ui"
    meta_html = f'<div><span class="label">Command ID</span><code>{command_id}</code></div>'
    return LOG_VIEWER_HTML.format(
        title=f"Command Log Viewer | {command_id}",
        heading=f"Command: {command_id}",
        trace_url=trace_url,
        terminal_statuses_json="['success','failed','killed']",
        meta_html=meta_html,
    )


@router.post(
    "/execution/{command_id}/kill",
    response_model=ApiResponse[CommandExecutionResponse],
    summary="Kill a running command",
)
async def kill_command(
    command_id: str,
    request: Request,
    force: bool = False,
    svc: CommandService = Depends(_get_command_service),
    current_user: Annotated[User, Depends(get_current_user(["command_api"]))] = None,
) -> ApiResponse[CommandExecutionResponse]:
    data = await svc.kill(command_id, force=force)
    return ApiResponse(data=data, request_id=_request_id(request))
