"""
app/api/v1/command.py

SSH command-execution endpoints — proxied to deploy-service (v1).
All endpoints require the ``command_api`` scope, except the unauthed HTML
log-viewer shell (whose polled /trace/ui carries its own token).
"""
from __future__ import annotations

import html
import re
from typing import Annotated

# Strict allowlist for command_id values accepted by the unauthed /view endpoint.
# uuid4 ids (hex chars + hyphens) always satisfy this pattern; injection payloads
# containing backticks, ${ }, slashes, angle brackets, etc. do not.
_COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.api.v1.deploy import get_deploy_token_manager
from app.clients.command_service_client import CommandServiceClient
from app.clients.dry_run_command_service_client import DryRunCommandServiceClient
from app.core.config import get_settings
from app.core.dependencies import (
    get_current_user,
    get_current_user_cookie_or_header,
)
from app.core.log_viewer_template import LOG_VIEWER_HTML
from app.domain.command_models import (
    CommandExecutionRequest,
    CommandExecutionResponse,
    CommandTraceResponse,
    CommandWhitelistConfig,
    OutputFormat,
    UserCommandWhitelist,
)
from app.domain.models import ApiResponse, User
from app.services.command_service import CommandService

router = APIRouter(prefix="/command", tags=["command"])


def _get_command_service() -> CommandService:
    """Build a CommandService backed by a live CommandServiceClient.

    Reuses the shared deploy-service token manager singleton (same upstream
    identity as the pipeline proxy).

    In dry-run the client is swapped for an in-memory stand-in and the token
    manager is never touched. CommandService itself is unchanged. See
    app/clients/dry_run_command_service_client.py."""
    settings = get_settings()
    if settings.DRY_RUN_MODE:
        return CommandService(DryRunCommandServiceClient())  # type: ignore[arg-type]
    client = CommandServiceClient(
        base_url=settings.DEPLOY_SERVICE_URL,
        token_manager=get_deploy_token_manager(),
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
    data = await svc.execute_command(body)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/execution/{command_id}",
    response_model=ApiResponse[CommandExecutionResponse],
    summary="Poll command execution result",
    description=(
        "Returns a command's current status and result.\n\n"
        "`format=raw` (the default) is the historical response. `format=json` "
        "additionally parses the command's stdout into `output_json`, and is "
        "accepted only for commands whose whitelist entry declares "
        '`output_format: "json"` (see GET /command/{command_name}/info) — '
        "asking for it on any other command is a 400 from deploy-service. "
        "`output` itself always keeps its raw string value; when parsing does "
        "not happen, `output_json_error` says why."
    ),
)
async def get_command_execution_status(
    command_id: str,
    request: Request,
    # Named `output_format` in Python but exposed as `?format=` via alias, to
    # match deploy-service's public query contract; the value is forwarded verbatim.
    output_format: OutputFormat = Query(
        default=OutputFormat.RAW,
        alias="format",
        description=(
            "raw = unchanged response; json = also parse stdout into "
            "output_json (requires the command to declare output_format json)."
        ),
    ),
    svc: CommandService = Depends(_get_command_service),
    current_user: Annotated[User, Depends(get_current_user(["command_api"]))] = None,
) -> ApiResponse[CommandExecutionResponse]:
    data = await svc.get_result(command_id, output_format)
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
    # Validate command_id before any HTML/JS rendering — reject ids whose charset
    # could break out of JS template-literal contexts (backticks, ${}, slashes, etc.).
    if not _COMMAND_ID_RE.fullmatch(command_id):
        from app.core.exceptions import NotFoundException
        raise NotFoundException("Command not found.")
    safe_id = html.escape(command_id)
    trace_url = f"/api/v1/command/execution/{command_id}/trace/ui"
    meta_html = f'<div><span class="label">Command ID</span><code>{safe_id}</code></div>'
    return LOG_VIEWER_HTML.format(
        title=f"Command Log Viewer | {safe_id}",
        heading=f"Command: {safe_id}",
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
