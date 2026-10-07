"""
tests/integration/test_dry_run_configmap_routes.py

GET /api/v1/clusters/{cluster}/configmaps, end to end through the real router
and ConfigMapService, with only the cluster replaced (DRY_RUN_MODE).

The listing is a lower privilege than reading content (CONTEXT.md, "ConfigMap
listing"), so the assertion that matters most is that no value and no
annotation reaches the response. Auth refusals live with every other router's
in test_dry_run_deny_paths.py.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings

_URL = "/api/v1/clusters/any-cluster/configmaps"


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture
def headers(client) -> dict[str, str]:
    resp = client.post("/token", data={"username": "test_admin", "password": "secret"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _list(client, headers, **params) -> list[dict]:
    resp = client.get(_URL, headers=headers, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["configmaps"]


def _ids(items: list[dict]) -> set[tuple[str, str]]:
    return {(c["namespace"], c["name"]) for c in items}


def test_lists_one_namespace(client, headers):
    items = _list(client, headers, namespace="apps")
    assert _ids(items) == {("apps", "dry-run-shared")}


def test_wildcard_lists_every_namespace(client, headers):
    ids = _ids(_list(client, headers, namespace="*"))
    assert ("default", "dry-run-app-config") in ids
    assert ("apps", "dry-run-shared") in ids


def test_same_name_in_two_namespaces_is_two_entries(client, headers):
    ids = _ids(_list(client, headers, namespace="*", name="dry-run-shared"))
    assert ids == {("default", "dry-run-shared"), ("apps", "dry-run-shared")}


def test_name_prefixes_are_comma_separated_and_ored(client, headers):
    ids = _ids(_list(client, headers, namespace="default", name="dry-run-app-,dry-run-bin"))
    assert ids == {("default", "dry-run-app-config"), ("default", "dry-run-binary")}


def test_summary_shape(client, headers):
    [binary] = [c for c in _list(client, headers, namespace="default") if c["name"] == "dry-run-binary"]
    assert binary["keys"] == ["README", "cert.der"]
    assert binary["labels"] == {"dry-run": "true"}
    assert binary["creation_timestamp"].startswith("2000-01-01")


def test_no_value_or_annotation_reaches_the_listing(client, headers):
    resp = client.get(_URL, headers=headers, params={"namespace": "*"})
    body = resp.text

    for value in ("debug", "8080", "AAEC", "warn", "dry-run-release"):
        assert value not in body, f"value {value!r} leaked into the listing"
    assert "last-applied-configuration" not in body
    assert "annotations" not in body


def test_unknown_namespace_is_an_empty_200(client, headers):
    assert _list(client, headers, namespace="no-such-namespace") == []


def test_namespace_is_required(client, headers):
    resp = client.get(_URL, headers=headers)
    assert resp.status_code == 422


def test_marks_the_response_as_dry_run(client, headers):
    resp = client.get(_URL, headers=headers, params={"namespace": "*"})
    assert resp.json()["dry_run"] is True


# ── blocking work stays off the event loop ────────────────────────────────────


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def test_client_construction_runs_off_the_event_loop(client, headers, monkeypatch):
    """Resolving credentials reads a kubeconfig and may run an exec credential
    plugin (EKS / GKE) that takes seconds; building the client can write a CA
    temp file. On the event loop either one stalls every request, health checks
    included — the same reason the service call itself is threaded."""
    from app.api.v1 import configmaps
    from app.repositories.dry_run_cluster_repository import DryRunClusterRepository
    from app.services.kube_client import KubeClientFactory

    seen: dict[str, bool] = {}

    class RecordingRepo(DryRunClusterRepository):
        def get_kube_client_config(self, cluster):
            seen["repo"] = _on_event_loop()
            return super().get_kube_client_config(cluster)

    class RecordingFactory(KubeClientFactory):
        def get_core_v1(self, cfg):
            seen["factory"] = _on_event_loop()
            return super().get_core_v1(cfg)

    client.app.dependency_overrides[configmaps._get_cluster_repo] = RecordingRepo
    monkeypatch.setattr(configmaps, "KubeClientFactory", RecordingFactory)
    try:
        resp = client.get(_URL, headers=headers, params={"namespace": "*"})
    finally:
        client.app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert seen == {"repo": False, "factory": False}
