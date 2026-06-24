"""
app/services/command_service.py

Thin orchestration layer over CommandServiceClient. Mirrors PipelineService:
keeps the router HTTP-only and the client easy to mock. All client exceptions
(DeployServiceError) propagate unchanged to the global handler.
"""
from __future__ import annotations

import logging

from app.clients.command_service_client import CommandServiceClient
from app.domain.command_models import (
    CommandExecutionRequest,
    CommandExecutionResponse,
    CommandTraceResponse,
    CommandWhitelistConfig,
    UserCommandWhitelist,
)

_logger = logging.getLogger(__name__)


class CommandService:
    """Orchestrates command-API calls by delegating to CommandServiceClient."""

    def __init__(self, client: CommandServiceClient) -> None:
        self._client = client

    async def get_all_commands(self) -> UserCommandWhitelist:
        return await self._client.get_all_commands_info()

    async def get_command(self, command_name: str) -> CommandWhitelistConfig:
        return await self._client.get_command_info(command_name)

    async def execute(self, body: CommandExecutionRequest) -> CommandExecutionResponse:
        _logger.info(
            "Proxying command execution | command=%s | host=%s",
            body.command_name, body.host,
        )
        return await self._client.execute_command(body)

    async def get_result(self, command_id: str) -> CommandExecutionResponse:
        return await self._client.get_command_result(command_id)

    async def kill(self, command_id: str, force: bool = False) -> CommandExecutionResponse:
        _logger.info("Proxying kill | command_id=%s | force=%s", command_id, force)
        return await self._client.kill_command(command_id, force=force)

    async def get_trace(
        self, command_id: str, byte_offset: int = 0, line_num: int = 1
    ) -> CommandTraceResponse:
        return await self._client.get_command_trace(command_id, byte_offset, line_num)
