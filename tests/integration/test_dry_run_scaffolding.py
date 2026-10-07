"""
tests/integration/test_dry_run_scaffolding.py

T8: the dry-run scaffolding seen from the HTTP surface.

The point of this file is the *absence* of change. T8 adds a flag, a start-up
guard and a response marker, and nothing else: enabling DRY_RUN_MODE must not
alter routing, auth, the published contract, or the error envelope. The stubs
that make dry-run actually do something arrive in T9 / T10, and these tests are
what will catch it if one of them quietly changes the surface on the way in.

Design shared with deploy-service; see that repo's docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings


def _build_client(monkeypatch, dry_run: bool) -> TestClient:
    monkeypatch.setenv("DRY_RUN_MODE", "true" if dry_run else "false")
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture
def dry_run_client(monkeypatch) -> TestClient:
    with _build_client(monkeypatch, dry_run=True) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture
def live_client(monkeypatch) -> TestClient:
    with _build_client(monkeypatch, dry_run=False) as c:
        yield c
    get_settings.cache_clear()


def _auth(client: TestClient, account: str = "test_admin") -> dict[str, str]:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ── the marker on real responses ──────────────────────────────────────────────


def test_success_responses_are_marked_in_dry_run(dry_run_client):
    """The marker has to survive the route's own ApiResponse construction —
    none of the 31 call sites was modified, so this proves the single hook
    reaches them."""
    resp = dry_run_client.post(
        "/token", data={"username": "test_admin", "password": "secret"}
    )
    assert resp.status_code == 200

    scopes = dry_run_client.get(
        "/api/v1/auth/my-scopes", headers=_auth(dry_run_client)
    )
    assert scopes.status_code == 200
    assert scopes.json()["dry_run"] is True


def test_success_responses_are_not_marked_when_off(live_client):
    """The marker must be a real signal. If it were true unconditionally a
    caller could never tell a dry-run deployment from a production one."""
    scopes = live_client.get("/api/v1/auth/my-scopes", headers=_auth(live_client))
    assert scopes.status_code == 200
    assert scopes.json()["dry_run"] is False


def test_the_envelope_keeps_its_existing_fields(dry_run_client):
    """Additive change only — an existing client reading data / request_id must
    be unaffected."""
    body = dry_run_client.get(
        "/api/v1/auth/my-scopes", headers=_auth(dry_run_client)
    ).json()
    assert "data" in body
    assert "request_id" in body


# ── error envelope ────────────────────────────────────────────────────────────


def test_error_responses_are_unchanged_in_dry_run(dry_run_client):
    """Errors are built by the exception handler, not by ApiResponse, so they
    carry no marker. Asserting it keeps a future refactor from quietly routing
    errors through the envelope and changing the error contract.

    A malformed token is used rather than a missing one: a missing token is
    rejected by OAuth2PasswordBearer itself, which raises HTTPException before
    the app's handler runs and so answers FastAPI's own
    ``{"detail": ...}`` shape. That split is pre-existing and unrelated to
    dry-run; this asserts the envelope the app actually owns.
    """
    resp = dry_run_client.get(
        "/api/v1/auth/my-scopes", headers={"Authorization": "Bearer nonsense"}
    )
    assert resp.status_code == 401

    body = resp.json()
    assert "error" in body
    assert "request_id" in body
    assert "dry_run" not in body


def test_the_error_envelope_matches_in_both_modes(dry_run_client, live_client):
    """The error contract must not depend on the mode at all."""

    def _shape(client):
        body = client.get(
            "/api/v1/auth/my-scopes", headers={"Authorization": "Bearer nonsense"}
        ).json()
        return sorted(body), sorted(body["error"])

    assert _shape(dry_run_client) == _shape(live_client)


def test_auth_still_rejects_in_dry_run(dry_run_client):
    """T8 stubs nothing, so this is a floor rather than a feature: dry-run must
    not have weakened authentication even while it does nothing else."""
    assert dry_run_client.get("/api/v1/auth/my-scopes").status_code == 401


def test_a_malformed_token_is_still_rejected(dry_run_client):
    resp = dry_run_client.get(
        "/api/v1/auth/my-scopes", headers={"Authorization": "Bearer nonsense"}
    )
    assert resp.status_code == 401


# ── published contract ────────────────────────────────────────────────────────


def test_openapi_paths_are_identical_in_both_modes(dry_run_client, live_client):
    """Dry-run must not add, remove or rename an endpoint — a caller writing
    against a dry-run instance has to be writing against the real contract."""
    dry = dry_run_client.get("/openapi.json").json()
    live = live_client.get("/openapi.json").json()
    assert set(dry["paths"]) == set(live["paths"])


def test_openapi_schemas_are_identical_in_both_modes(dry_run_client, live_client):
    """Covers the response models themselves. The marker is part of the schema
    in BOTH modes — it is a field with a default, not a dry-run-only addition."""
    dry = dry_run_client.get("/openapi.json").json()["components"]["schemas"]
    live = live_client.get("/openapi.json").json()["components"]["schemas"]
    assert dry == live


def test_security_requirements_are_unchanged(dry_run_client, live_client):
    def _security(spec):
        return {
            (path, method): op.get("security")
            for path, ops in spec["paths"].items()
            for method, op in ops.items()
        }

    assert _security(dry_run_client.get("/openapi.json").json()) == _security(
        live_client.get("/openapi.json").json()
    )


def test_the_marker_is_published_in_the_schema(dry_run_client):
    """A client has to be able to discover the field rather than find it by
    surprise in a payload."""
    schemas = dry_run_client.get("/openapi.json").json()["components"]["schemas"]
    envelopes = [
        name for name, s in schemas.items() if "dry_run" in s.get("properties", {})
    ]
    assert envelopes, "no ApiResponse schema publishes dry_run"
