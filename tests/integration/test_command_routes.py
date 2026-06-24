"""Integration tests for the command proxy router. CommandService is overridden
with a fake via dependency_overrides so no real deploy-service is needed."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from app.main import app
from app.api.v1.command import _get_command_service
from app.domain.command_models import (
    CommandExecutionResponse,
    CommandWhitelistConfig,
    PipelineStep,
    UserCommandWhitelist,
)


def _login(client, username="test_admin", password="secret") -> str:
    r = client.post("/token", data={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture
def fake_service():
    svc = AsyncMock()
    svc.get_all_commands = AsyncMock(
        return_value=UserCommandWhitelist(name="cluster_proxy", allow_commands=[])
    )
    svc.execute = AsyncMock(
        return_value=CommandExecutionResponse(command_id="abc", status="running")
    )
    svc.get_result = AsyncMock(
        return_value=CommandExecutionResponse(command_id="abc", status="success")
    )
    svc.get_command = AsyncMock(
        return_value=CommandWhitelistConfig(
            command_name="run_ansible", pipeline=[]
        )
    )
    svc.kill = AsyncMock(
        return_value=CommandExecutionResponse(command_id="abc", status="accepted")
    )
    app.dependency_overrides[_get_command_service] = lambda: svc
    yield svc
    app.dependency_overrides.pop(_get_command_service, None)


def test_info_requires_auth(client):
    r = client.get("/api/v1/command/info")
    assert r.status_code == 401


def test_info_returns_whitelist(client, fake_service):
    token = _login(client)
    r = client.get("/api/v1/command/info", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["name"] == "cluster_proxy"


def test_execute_proxies(client, fake_service):
    token = _login(client)
    r = client.post(
        "/api/v1/command/execution",
        headers={"Authorization": f"Bearer {token}"},
        json={"command_name": "run_ansible", "host": "1.2.3.4", "username": "root"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["command_id"] == "abc"


def test_view_is_unauthed_html(client):
    r = client.get("/api/v1/command/execution/abc/view")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "abc" in r.text


def test_kill_proxies(client, fake_service):
    token = _login(client)
    r = client.post(
        "/api/v1/command/execution/abc/kill",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    fake_service.kill.assert_awaited_once()


def test_get_execution_status(client, fake_service):
    token = _login(client)
    r = client.get(
        "/api/v1/command/execution/abc",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["command_id"] == "abc"
    assert r.json()["data"]["status"] == "success"


def test_get_command_info(client, fake_service):
    token = _login(client)
    r = client.get(
        "/api/v1/command/run_ansible/info",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["command_name"] == "run_ansible"


def test_view_escapes_malicious_id(client):
    r = client.get("/api/v1/command/execution/%3Cscript%3E/view")
    assert r.status_code == 200
    # The escaped form must appear in HTML attributes (title, heading, meta).
    assert "&lt;script&gt;" in r.text
    # The raw tag must NOT appear in the title element (the primary XSS vector).
    assert "<title>Command Log Viewer | <script>" not in r.text
    # The raw tag must NOT appear in the h1 heading.
    assert "<h1>Command: <script>" not in r.text
    # The raw tag must NOT appear in the meta_html code element.
    assert "<code><script>" not in r.text
