"""
tests/integration/test_dry_run_configmap_routes.py

Both ConfigMap routes, end to end through the real router and
ConfigMapService, with only the cluster replaced (DRY_RUN_MODE):

  GET …/configmaps                                — listing (cluster_api)
  GET …/namespaces/{namespace}/configmaps/{name}  — content (+ configmap_read)

Listing is a lower privilege than reading content (CONTEXT.md), so the
assertions that matter most are negative: no value or annotation in a listing,
no stale last-applied copy in content. Generic 401 / 403 refusals live with
every other router's in test_dry_run_deny_paths.py; the scope tests here are
the ones that express the relationship between the two privileges — what
cluster_api alone can and cannot do, and that a missing ConfigMap cannot be
probed without configmap_read.
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


# ══ content: GET …/namespaces/{namespace}/configmaps/{name} ═══════════════════
#
# Needs cluster_api AND configmap_read: configmap_read is a step above cluster
# access, not a separate way in. test_operator holds cluster_api only.

_CONTENT = "/api/v1/clusters/any-cluster/namespaces/{ns}/configmaps/{name}"


def _content_url(ns: str, name: str) -> str:
    return _CONTENT.format(ns=ns, name=name)


@pytest.fixture
def operator_headers(client) -> dict[str, str]:
    resp = client.post("/token", data={"username": "test_operator", "password": "secret"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _read(client, headers, ns: str, name: str) -> dict:
    resp = client.get(_content_url(ns, name), headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def test_reads_values_and_binary_data(client, headers):
    data = _read(client, headers, "default", "dry-run-binary")
    assert data["name"] == "dry-run-binary"
    assert data["namespace"] == "default"
    assert data["data"] == {"README": "see cert.der"}
    assert data["binary_data"] == {"cert.der": "AAEC"}
    assert data["labels"] == {"dry-run": "true"}
    assert data["creation_timestamp"].startswith("2000-01-01")


def test_last_applied_is_stripped_and_other_annotations_survive(client, headers):
    data = _read(client, headers, "default", "dry-run-applied")
    assert data["annotations"] == {"meta.helm.sh/release-name": "dry-run-release"}
    assert data["data"] == {"LOG_LEVEL": "warn"}


def test_the_stale_last_applied_value_appears_nowhere(client, headers):
    """last-applied says LOG_LEVEL=info while data says warn; only the current
    value may reach the caller."""
    body = client.get(_content_url("default", "dry-run-applied"), headers=headers).text
    assert '"info"' not in body
    assert "last-applied-configuration" not in body
    assert "managed" not in body.lower()


def test_same_name_in_two_namespaces_reads_each_ones_own_values(client, headers):
    assert _read(client, headers, "default", "dry-run-shared")["data"] == {"TEAM": "default"}
    assert _read(client, headers, "apps", "dry-run-shared")["data"] == {"TEAM": "apps"}


@pytest.mark.parametrize(
    "ns, name",
    [("default", "no-such-configmap"), ("no-such-namespace", "dry-run-app-config")],
)
def test_missing_configmap_or_namespace_is_404_naming_both(client, headers, ns, name):
    resp = client.get(_content_url(ns, name), headers=headers)
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["code"] == "CONFIGMAP_NOT_FOUND"
    assert ns in error["message"] and name in error["message"]


def test_wildcard_namespace_is_rejected(client, headers):
    """The namespace is part of a ConfigMap's identity, not a filter."""
    resp = client.get(_content_url("*", "dry-run-shared"), headers=headers)
    assert resp.status_code == 422


def test_cluster_api_alone_can_list_but_not_read(client, operator_headers):
    listing = client.get(_URL, headers=operator_headers, params={"namespace": "*"})
    assert listing.status_code == 200

    resp = client.get(_content_url("default", "dry-run-app-config"), headers=operator_headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_a_missing_configmap_is_403_not_404_without_the_scope(client, operator_headers):
    """The scope check runs first, so a caller without configmap_read cannot
    probe which ConfigMaps exist by telling 404 from 403."""
    resp = client.get(_content_url("default", "no-such-configmap"), headers=operator_headers)
    assert resp.status_code == 403


# ── blocking work stays off the event loop (both routes) ──────────────────────


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.mark.parametrize(
    "url, params",
    [
        (_URL, {"namespace": "*"}),
        (_content_url("default", "dry-run-app-config"), {}),
    ],
    ids=["listing", "content"],
)
def test_client_construction_runs_off_the_event_loop(client, headers, monkeypatch, url, params):
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
        resp = client.get(url, headers=headers, params=params)
    finally:
        client.app.dependency_overrides.clear()

    assert resp.status_code == 200
    assert seen == {"repo": False, "factory": False}
