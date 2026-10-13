"""
tests/integration/test_dry_run_namespace_routes.py

The namespace routes, end to end through the real router and NamespaceService,
with only the cluster replaced (DRY_RUN_MODE). Vocabulary follows CONTEXT.md,
"Namespaces". Generic 401 / 403 refusals live with every other router's in
test_dry_run_deny_paths.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.services.dry_run_kube_client import reset_dry_run_clusters

_URL = "/api/v1/clusters/any-cluster/namespaces"


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    get_settings.cache_clear()
    reset_dry_run_clusters()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    reset_dry_run_clusters()
    get_settings.cache_clear()


@pytest.fixture
def headers(client) -> dict[str, str]:
    resp = client.post("/token", data={"username": "test_admin", "password": "secret"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ── reading one namespace ─────────────────────────────────────────────────────


def test_reads_an_active_namespace(client, headers):
    resp = client.get(f"{_URL}/default", headers=headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["name"] == "default"
    assert data["status"] == "Active"
    assert data["deletion_timestamp"] is None


def test_a_terminating_namespace_shows_when_deletion_was_requested(client, headers):
    resp = client.get(f"{_URL}/dry-run-terminating", headers=headers)

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status"] == "Terminating"
    assert data["deletion_timestamp"] is not None


def test_last_applied_is_left_out_of_a_read(client, headers):
    data = client.get(f"{_URL}/dry-run-apps", headers=headers).json()["data"]

    assert "kubectl.kubernetes.io/last-applied-configuration" not in data["annotations"]
    # Only that one annotation is stripped, not all of them.
    assert data["annotations"]["meta.helm.sh/release-name"] == "dry-run-release"


def test_a_missing_namespace_is_404(client, headers):
    resp = client.get(f"{_URL}/no-such-namespace", headers=headers)

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NAMESPACE_NOT_FOUND"


# ── listing ───────────────────────────────────────────────────────────────────


def _list(client, headers, **params) -> list[str]:
    resp = client.get(_URL, headers=headers, params=params)
    assert resp.status_code == 200, resp.text
    return [n["name"] for n in resp.json()["data"]["namespaces"]]


def test_lists_every_namespace(client, headers):
    names = _list(client, headers)
    assert {"default", "kube-system", "dry-run-apps", "dry-run-terminating"} <= set(names)


def test_lists_by_comma_separated_name_prefixes(client, headers):
    assert set(_list(client, headers, name="kube-,dry-run-a")) == {"kube-system", "dry-run-apps"}


def test_lists_by_status(client, headers):
    assert _list(client, headers, status="terminating") == ["dry-run-terminating"]


def test_lists_by_label_selector(client, headers):
    assert _list(client, headers, label_selector="kubernetes.io/metadata.name=default") == ["default"]


def test_an_unknown_status_is_422(client, headers):
    resp = client.get(_URL, headers=headers, params={"status": "Deleted"})
    assert resp.status_code == 422


def test_a_malformed_label_selector_is_422(client, headers):
    resp = client.get(_URL, headers=headers, params={"label_selector": "=oops"})
    assert resp.status_code == 422
