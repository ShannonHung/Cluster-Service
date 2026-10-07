"""
tests/integration/test_dry_run_proxy_routes.py

T9: every deploy, command and inventory proxy endpoint answers in dry-run with
no deploy-service reachable and no deploy-service credentials configured.

"No upstream" is enforced, not assumed: the fixture makes any outbound httpx
request fail the test and blanks DEPLOY_SERVICE_PASSWORD / _TOKEN, so a 200
here cannot have come from a real call or a real token fetch.

As with the node routes, the 200s are the least interesting assertions. The
deny paths (401 / 403 / 422) prove auth and validation still run in front of
the stub, and the upstream-error paths prove the DeployServiceError adapter is
still reached rather than short-circuited.

No ``e2e`` marker: these need no upstream.

Design shared with deploy-service; see that repo's docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.clients.dry_run_command_service_client import (
    DRY_RUN_FACTS,
    DRY_RUN_PING,
    reset_dry_run_command_service,
)
from app.clients.dry_run_deploy_service_client import reset_dry_run_deploy_service
from app.core.config import get_settings


def _no_network(*_args, **_kwargs):
    raise AssertionError("dry-run made an outbound HTTP request")


@pytest.fixture
def dry_run_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("DEPLOY_SERVICE_PASSWORD", "")
    monkeypatch.setenv("DEPLOY_SERVICE_TOKEN", "")
    monkeypatch.setenv("DEPLOY_SERVICE_URL", "http://deploy-service.invalid")
    get_settings.cache_clear()
    # TestClient itself is built on httpx, so block at the async client used
    # by the real deploy-service clients and token manager, not httpx.Client.
    monkeypatch.setattr(httpx.AsyncClient, "send", _no_network)

    import app.api.v1.deploy as deploy_module

    # The real token-manager singleton must never even be constructed.
    monkeypatch.setattr(deploy_module, "_deploy_token_manager", None)

    reset_dry_run_deploy_service()
    reset_dry_run_command_service()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c

    assert deploy_module._deploy_token_manager is None, (
        "dry-run constructed the deploy-service token manager"
    )
    reset_dry_run_deploy_service()
    reset_dry_run_command_service()
    get_settings.cache_clear()


def _headers(*scopes: str) -> dict[str, str]:
    """Mint a token directly. No fixture user holds ``deploy_api``, and the
    scope check reads the token's claims, so this exercises the same check a
    real login would without widening the shared users file."""
    from app.core.security import create_access_token

    token = create_access_token({"sub": "dry-run-e2e", "scopes": list(scopes)})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def deploy_h() -> dict[str, str]:
    return _headers("deploy_api")


@pytest.fixture
def command_h() -> dict[str, str]:
    return _headers("command_api")


@pytest.fixture
def inventory_h() -> dict[str, str]:
    return _headers("inventory_api")


def _upstream_code(resp) -> str:
    return resp.json()["error"]["detail"]["error_code"]


# ── deploy ────────────────────────────────────────────────────────────────────


def test_pipeline_lifecycle(dry_run_client, deploy_h):
    trigger = dry_run_client.post(
        "/api/v1/deploy",
        params={"action": "test-deploy", "ref_name": "main"},
        json={"variables": [{"key": "TARGET", "value": "x"}]},
        headers=deploy_h,
    )
    assert trigger.status_code == 200, trigger.text
    body = trigger.json()
    assert body["dry_run"] is True
    pipeline_id = body["data"]["id"]
    assert body["data"]["status"] == "running"

    running = dry_run_client.post(
        "/api/v1/deploy/check-running",
        params={"action": "test-deploy", "ref_name": "main"},
        json={"variables": [{"key": "TARGET", "value": "x"}]},
        headers=deploy_h,
    )
    assert running.status_code == 200
    assert running.json()["data"]["count"] == 1

    cancel = dry_run_client.post(f"/api/v1/deploy/{pipeline_id}/cancel", headers=deploy_h)
    assert cancel.status_code == 200
    assert cancel.json()["data"]["status"] == "canceled"

    status = dry_run_client.get(f"/api/v1/deploy/{pipeline_id}", headers=deploy_h)
    assert status.status_code == 200
    assert status.json()["data"]["status"] == "canceled", "cancel did not persist"

    retry = dry_run_client.post(f"/api/v1/deploy/{pipeline_id}/retry", headers=deploy_h)
    assert retry.status_code == 200
    assert retry.json()["data"]["status"] == "running"


def test_status_poll_terminates(dry_run_client, deploy_h):
    pipeline_id = dry_run_client.post(
        "/api/v1/deploy", params={"action": "a"}, headers=deploy_h
    ).json()["data"]["id"]
    status = dry_run_client.get(f"/api/v1/deploy/{pipeline_id}", headers=deploy_h)
    assert status.json()["data"]["status"] == "success"


def test_duplicate_trigger_goes_through_the_error_adapter(dry_run_client, deploy_h):
    """If dry-run short-circuited the client, this would be a second 200."""
    dry_run_client.post("/api/v1/deploy", params={"action": "a"}, headers=deploy_h)
    dup = dry_run_client.post("/api/v1/deploy", params={"action": "a"}, headers=deploy_h)
    assert dup.status_code == 502
    assert _upstream_code(dup) == "PIPELINE_CONFLICT"


def test_unknown_pipeline_goes_through_the_error_adapter(dry_run_client, deploy_h):
    resp = dry_run_client.get("/api/v1/deploy/1", headers=deploy_h)
    assert resp.status_code == 502
    assert _upstream_code(resp) == "PIPELINE_NOT_FOUND"


def test_deploy_still_requires_a_token(dry_run_client):
    assert dry_run_client.get("/api/v1/deploy/1").status_code == 401


def test_deploy_still_requires_the_deploy_scope(dry_run_client, command_h):
    resp = dry_run_client.post("/api/v1/deploy", params={"action": "a"}, headers=command_h)
    assert resp.status_code == 403


def test_deploy_still_validates_input(dry_run_client, deploy_h):
    missing_action = dry_run_client.post("/api/v1/deploy", headers=deploy_h)
    bad_id = dry_run_client.get("/api/v1/deploy/not-an-int", headers=deploy_h)
    assert missing_action.status_code == 422
    assert bad_id.status_code == 422


# ── command ───────────────────────────────────────────────────────────────────


def _execute(client, headers, command=DRY_RUN_PING, **extra) -> str:
    resp = client.post(
        "/api/v1/command/execution",
        json={"command_name": command, "host": "10.0.0.1", "username": "u", **extra},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["command_id"]


def test_command_listing(dry_run_client, command_h):
    resp = dry_run_client.get("/api/v1/command/info", headers=command_h)
    assert resp.status_code == 200
    assert resp.json()["data"]["allow_commands"], "an empty whitelist proves nothing"

    one = dry_run_client.get(f"/api/v1/command/{DRY_RUN_PING}/info", headers=command_h)
    assert one.status_code == 200
    assert one.json()["data"]["command_name"] == DRY_RUN_PING


def test_command_lifecycle(dry_run_client, command_h):
    command_id = _execute(dry_run_client, command_h)

    kill = dry_run_client.post(
        f"/api/v1/command/execution/{command_id}/kill",
        params={"force": "true"},
        headers=command_h,
    )
    assert kill.status_code == 200
    assert kill.json()["data"]["status"] == "killed"

    poll = dry_run_client.get(f"/api/v1/command/execution/{command_id}", headers=command_h)
    assert poll.status_code == 200
    assert poll.json()["data"]["status"] == "killed"


def test_command_poll_with_json_format(dry_run_client, command_h):
    command_id = _execute(dry_run_client, command_h, DRY_RUN_FACTS)
    poll = dry_run_client.get(
        f"/api/v1/command/execution/{command_id}",
        params={"format": "json"},
        headers=command_h,
    )
    assert poll.status_code == 200
    assert poll.json()["data"]["output_json"]["dry_run"] is True


def test_trace_and_viewer_return_content(dry_run_client, command_h):
    command_id = _execute(dry_run_client, command_h)

    trace = dry_run_client.get(
        f"/api/v1/command/execution/{command_id}/trace/ui", headers=command_h
    )
    assert trace.status_code == 200
    assert trace.json()["data"]["lines"], "the viewer would show an empty log"

    view = dry_run_client.get(f"/api/v1/command/execution/{command_id}/view")
    assert view.status_code == 200
    assert command_id in view.text


def test_trace_accepts_the_cookie(dry_run_client, command_h):
    """The browser viewer authenticates /trace/ui by cookie, not header."""
    command_id = _execute(dry_run_client, command_h)
    token = command_h["Authorization"].removeprefix("Bearer ")
    dry_run_client.cookies.set("access_token", token)
    try:
        trace = dry_run_client.get(f"/api/v1/command/execution/{command_id}/trace/ui")
    finally:
        dry_run_client.cookies.clear()
    assert trace.status_code == 200


def test_non_whitelisted_command_goes_through_the_error_adapter(dry_run_client, command_h):
    resp = dry_run_client.post(
        "/api/v1/command/execution",
        json={"command_name": "rm_rf", "host": "10.0.0.1", "username": "u"},
        headers=command_h,
    )
    assert resp.status_code == 502
    assert _upstream_code(resp) == "DEPLOY_SERVICE_FORBIDDEN"


def test_command_still_requires_a_token(dry_run_client):
    assert dry_run_client.get("/api/v1/command/info").status_code == 401


def test_command_still_requires_the_command_scope(dry_run_client, deploy_h):
    assert dry_run_client.get("/api/v1/command/info", headers=deploy_h).status_code == 403


def test_command_still_validates_input(dry_run_client, command_h):
    resp = dry_run_client.post(
        "/api/v1/command/execution", json={"command_name": DRY_RUN_PING}, headers=command_h
    )
    assert resp.status_code == 422


def test_viewer_still_rejects_unsafe_ids(dry_run_client):
    assert dry_run_client.get("/api/v1/command/execution/a$b/view").status_code == 404


# ── inventory ─────────────────────────────────────────────────────────────────


def test_inventory_endpoints(dry_run_client, inventory_h):
    node = dry_run_client.get(
        "/api/v1/inventory/nodes/dry-run-node-ready", headers=inventory_h
    )
    assert node.status_code == 200, node.text

    mappings = dry_run_client.get(
        "/api/v1/inventory/mappings", params={"type": "dry-run"}, headers=inventory_h
    )
    assert mappings.status_code == 200 and mappings.json()["data"]

    node_bastion = dry_run_client.get(
        "/api/v1/inventory/nodes/dry-run-node-ready/bastion-resolution",
        headers=inventory_h,
    )
    assert node_bastion.status_code == 200

    cluster_bastion = dry_run_client.get(
        "/api/v1/inventory/cluster/bastion-resolution",
        params={"cluster_name": "dry-run-cluster"},
        headers=inventory_h,
    )
    assert cluster_bastion.status_code == 200


def test_inventory_unknown_node_is_404(dry_run_client, inventory_h):
    """The service's upstream-404 → 404 translation is still on the path."""
    resp = dry_run_client.get("/api/v1/inventory/nodes/no-such-node", headers=inventory_h)
    assert resp.status_code == 404


def test_inventory_still_requires_its_scope(dry_run_client, deploy_h):
    resp = dry_run_client.get("/api/v1/inventory/nodes/dry-run-node-ready", headers=deploy_h)
    assert resp.status_code == 403
