"""
app/clients/command_service_client.py

Async HTTP client for deploy-service's SSH command-execution API, with automatic
token management and one-time 401 retry. Mirrors DeployServiceClient's transport
behaviour; kept separate because pipelines and SSH commands are distinct domains.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.core.exceptions import DeployServiceError
from app.core.token_manager import TokenManager
from app.domain.command_models import (
    CommandExecutionRequest,
    CommandExecutionResponse,
    CommandTraceResponse,
    CommandWhitelistConfig,
    UserCommandWhitelist,
)

_logger = logging.getLogger(__name__)


class CommandServiceClient:
    """Async client that forwards command-API calls to deploy-service."""

    def __init__(
        self,
        base_url: str,
        token_manager: TokenManager,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token_manager = token_manager
        self._timeout = timeout
        self._transport: Optional[httpx.BaseTransport] = None  # tests inject MockTransport

    # ── private helpers ───────────────────────────────────────────────────────

    async def _headers(self) -> dict[str, str]:
        token = await self._token_manager.get_token()
        return {"Authorization": f"Bearer {token}"}

    def _raise_for_error(self, response: httpx.Response, context: str) -> None:
        try:
            body: dict[str, Any] = response.json()
        except Exception:
            body = {}
        _logger.error(
            "deploy-service command error | context=%s | status=%s | body=%s",
            context, response.status_code, body,
        )
        raise DeployServiceError(http_status=response.status_code, body=body)

    async def _request_with_retry(
        self, method: str, path: str, context: str, **kwargs
    ) -> dict[str, Any]:
        headers = await self._headers()
        kwargs["headers"] = {**kwargs.get("headers", {}), **headers}

        client_kwargs = dict(base_url=self._base_url, timeout=self._timeout)
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.request(method, path, **kwargs)

            if response.status_code == 401:
                _logger.warning(
                    "Received 401 from deploy-service during %s. Refreshing token and retrying...",
                    context,
                )
                await self._token_manager.refresh()
                headers = await self._headers()
                kwargs["headers"] = {**kwargs.get("headers", {}), **headers}
                response = await client.request(method, path, **kwargs)

        if response.is_error:
            self._raise_for_error(response, context)

        return response.json()

    # ── public API ────────────────────────────────────────────────────────────

    async def get_all_commands_info(self) -> UserCommandWhitelist:
        raw = await self._request_with_retry(
            "GET", "/api/v1/command/info", context="get_all_commands_info"
        )
        return UserCommandWhitelist(**raw["data"])

    async def get_command_info(self, command_name: str) -> CommandWhitelistConfig:
        raw = await self._request_with_retry(
            "GET", f"/api/v1/command/{command_name}/info", context="get_command_info"
        )
        return CommandWhitelistConfig(**raw["data"])

    async def execute_command(
        self, body: CommandExecutionRequest
    ) -> CommandExecutionResponse:
        raw = await self._request_with_retry(
            "POST", "/api/v1/command/execution",
            context="execute_command",
            json=body.model_dump(mode="json"),
        )
        return CommandExecutionResponse(**raw["data"])

    async def get_command_result(self, command_id: str) -> CommandExecutionResponse:
        raw = await self._request_with_retry(
            "GET", f"/api/v1/command/execution/{command_id}",
            context="get_command_result",
        )
        return CommandExecutionResponse(**raw["data"])

    async def kill_command(
        self, command_id: str, force: bool = False
    ) -> CommandExecutionResponse:
        raw = await self._request_with_retry(
            "POST", f"/api/v1/command/execution/{command_id}/kill",
            context="kill_command",
            params={"force": force},
        )
        return CommandExecutionResponse(**raw["data"])

    async def get_command_trace(
        self, command_id: str, byte_offset: int = 0, line_num: int = 1
    ) -> CommandTraceResponse:
        raw = await self._request_with_retry(
            "GET", f"/api/v1/command/execution/{command_id}/trace/ui",
            context="get_command_trace",
            params={"byte_offset": byte_offset, "line_num": line_num},
        )
        return CommandTraceResponse(**raw["data"])
