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


def test_reading_a_name_no_namespace_could_have_is_422(client, headers):
    resp = client.get(f"{_URL}/Not_A_DNS_Label", headers=headers)
    assert resp.status_code == 422


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


# ── creating ──────────────────────────────────────────────────────────────────


def _create(client, headers, body):
    return client.post(_URL, headers=headers, json=body)


def test_creates_a_namespace_then_reads_and_lists_it(client, headers):
    resp = _create(client, headers, {"name": "team-a"})

    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["name"] == "team-a"
    assert resp.json()["data"]["status"] == "Active"
    assert client.get(f"{_URL}/team-a", headers=headers).status_code == 200
    assert "team-a" in _list(client, headers)


def test_a_created_namespace_carries_nothing_from_the_service(client, headers):
    data = _create(client, headers, {"name": "team-a"}).json()["data"]

    assert data["annotations"] == {}
    # Kubernetes itself labels every namespace with its own name; nothing else.
    assert set(data["labels"]) <= {"kubernetes.io/metadata.name"}


def test_creating_the_same_name_twice_is_409(client, headers):
    _create(client, headers, {"name": "team-a"})
    resp = _create(client, headers, {"name": "team-a"})

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "NAMESPACE_ALREADY_EXISTS"
    assert error["detail"]["status"] == "Active"


def test_creating_a_terminating_name_says_it_is_being_deleted(client, headers):
    resp = _create(client, headers, {"name": "dry-run-terminating"})

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "NAMESPACE_ALREADY_EXISTS"
    assert error["detail"]["status"] == "Terminating"
    assert "being deleted" in error["message"]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "team-a", "labels": {"pod-security.kubernetes.io/enforce": "privileged"}},
        {"name": "team-a", "lables": {}},  # a typo is refused, not silently dropped
        {},
    ],
    ids=["labels", "unknown-field", "no-name"],
)
def test_a_body_with_anything_but_a_name_is_422(client, headers, body):
    assert _create(client, headers, body).status_code == 422


@pytest.mark.parametrize(
    "name",
    ["Team-A", "team_a", "-team", "team-", "a" * 64, ""],
    ids=["uppercase", "underscore", "leading-dash", "trailing-dash", "64-chars", "empty"],
)
def test_a_name_that_is_not_a_dns_label_is_422(client, headers, name):
    assert _create(client, headers, {"name": name}).status_code == 422


@pytest.mark.parametrize("name", ["default", "kube-system", "kube-anything"])
def test_a_reserved_name_is_422(client, headers, name):
    resp = _create(client, headers, {"name": name})
    assert resp.status_code == 422


def test_a_refused_name_never_reaches_kubernetes(client, headers):
    _create(client, headers, {"name": "kube-new"})
    _create(client, headers, {"name": "Bad_Name"})

    names = _list(client, headers)
    assert "kube-new" not in names
    assert "Bad_Name" not in names
