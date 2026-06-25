"""Unit tests for CommandService — CommandServiceClient fully mocked."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.core.exceptions import DeployServiceError
from app.domain.command_models import (
    CommandExecutionRequest,
    CommandExecutionResponse,
    UserCommandWhitelist,
)
from app.services.command_service import CommandService


def _service(**overrides) -> CommandService:
    client = MagicMock()
    client.get_all_commands_info = AsyncMock(
        return_value=overrides.get(
            "all", UserCommandWhitelist(name="cluster_proxy", allow_commands=[])
        )
    )
    client.execute_command = AsyncMock(
        return_value=overrides.get(
            "exec", CommandExecutionResponse(command_id="abc", status="running")
        )
    )
    client.get_command_result = AsyncMock(
        return_value=overrides.get(
            "result", CommandExecutionResponse(command_id="abc", status="success")
        )
    )
    client.kill_command = AsyncMock(
        return_value=overrides.get(
            "kill", CommandExecutionResponse(command_id="abc", status="accepted")
        )
    )
    return CommandService(client=client)


async def test_get_all_commands_delegates():
    svc = _service(all=UserCommandWhitelist(name="cluster_proxy", allow_commands=[]))
    wl = await svc.get_all_commands()
    assert wl.name == "cluster_proxy"


async def test_execute_delegates_and_passes_body():
    svc = _service()
    body = CommandExecutionRequest(command_name="run_ansible", host="1.2.3.4", username="root")
    resp = await svc.execute(body)
    assert resp.command_id == "abc"
    svc._client.execute_command.assert_awaited_once_with(body)


async def test_kill_forwards_force_flag():
    svc = _service()
    await svc.kill("abc", force=True)
    svc._client.kill_command.assert_awaited_once_with("abc", force=True)


async def test_propagates_deploy_service_error():
    svc = _service()
    svc._client.get_command_result = AsyncMock(
        side_effect=DeployServiceError(http_status=404, body={"error": {"code": "NOT_FOUND"}})
    )
    with pytest.raises(DeployServiceError):
        await svc.get_result("missing")
