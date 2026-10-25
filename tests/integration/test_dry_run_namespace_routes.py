"""
tests/integration/test_dry_run_namespace_routes.py

The namespace routes, end to end through the real router and NamespaceService,
with only the cluster replaced (DRY_RUN_MODE). Vocabulary follows CONTEXT.md,
"Namespaces". Generic 401 / 403 refusals live with every other router's in
test_dry_run_deny_paths.py.
"""

from __future__ import annotations

import re

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


def test_a_created_namespace_is_stamped_with_its_provenance(client, headers):
    """ADR 0002, under .env.test's prefix. Visible on a following read, not only
    in the create response — it is on the namespace, not just in the answer."""
    resp = client.post(_URL, headers={**headers, "X-Coordination-ID": "req-42"}, json={"name": "team-a"})
    assert resp.status_code == 201, resp.text

    annotations = client.get(f"{_URL}/team-a", headers=headers).json()["data"]["annotations"]
    assert annotations["cluster-service.test/created-by"] == "test_admin"
    assert annotations["cluster-service.test/last-request-id"] == "req-42"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", annotations["cluster-service.test/created-at"])
    assert set(annotations) == {
        "cluster-service.test/created-by",
        "cluster-service.test/created-at",
        "cluster-service.test/last-request-id",
    }


def test_a_created_namespace_carries_no_label_of_the_service(client, headers):
    data = _create(client, headers, {"name": "team-a"}).json()["data"]

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
        {"name": "team-a", "lables": {}},  # a typo is refused, not silently dropped
        {"name": "team-a", "annotations": {"a": "b"}},
        {},
    ],
    ids=["unknown-field", "annotations", "no-name"],
)
def test_a_body_with_anything_but_a_name_and_labels_is_422(client, headers, body):
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


# ── labels on create ──────────────────────────────────────────────────────────


def test_creates_a_namespace_with_labels(client, headers):
    labels = {"team": "a", "acme.io/cost-center": "r-and-d", "empty-ok": ""}
    resp = _create(client, headers, {"name": "team-a", "labels": labels})

    assert resp.status_code == 201, resp.text
    read = client.get(f"{_URL}/team-a", headers=headers).json()["data"]["labels"]
    assert labels.items() <= read.items()
    assert "team-a" in _list(client, headers, label_selector="team=a")


def test_reserved_label_keys_are_422_naming_every_one(client, headers):
    """CONTEXT.md, Reserved label key: Kubernetes acts on these, so setting one
    would change the namespace's security posture. All named at once."""
    labels = {
        "pod-security.kubernetes.io/enforce": "privileged",
        "k8s.io/anything": "x",
        "kubernetes.io/metadata.name": "other",
        "team": "a",
    }
    resp = _create(client, headers, {"name": "team-a", "labels": labels})

    assert resp.status_code == 422
    body = resp.text
    for key in ("pod-security.kubernetes.io/enforce", "k8s.io/anything", "kubernetes.io/metadata.name"):
        assert key in body
    assert "team-a" not in _list(client, headers)


@pytest.mark.parametrize(
    "labels",
    [
        {"": "x"},
        {"Bad Key": "x"},
        {"-team": "x"},
        {"a" * 64: "x"},
        {"Acme.io/team": "x"},  # prefix must be lowercase
        {"acme.io/": "x"},
        {"a/b/c": "x"},
        {"team": "has space"},
        {"team": "a" * 64},
        {"team": "-a"},
    ],
    ids=[
        "empty-key", "space-in-key", "leading-dash", "64-char-name", "uppercase-prefix",
        "empty-name", "two-slashes", "space-in-value", "64-char-value", "leading-dash-value",
    ],
)
def test_a_malformed_label_is_422_and_nothing_is_created(client, headers, labels):
    resp = _create(client, headers, {"name": "team-a", "labels": labels})

    assert resp.status_code == 422
    assert "team-a" not in _list(client, headers)



# ── updating labels ───────────────────────────────────────────────────────────


def _patch(client, headers, name, body, request_id=None):
    if request_id:
        headers = {**headers, "X-Coordination-ID": request_id}
    return client.patch(f"{_URL}/{name}/labels", headers=headers, json=body)


def _read(client, headers, name):
    return client.get(f"{_URL}/{name}", headers=headers).json()["data"]


def test_set_adds_and_overwrites_remove_deletes(client, headers):
    _create(client, headers, {"name": "team-a", "labels": {"team": "a", "tier": "gold", "old": "x"}})

    resp = _patch(client, headers, "team-a", {"set": {"tier": "silver", "env": "prod"}, "remove": ["old"]})

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert (data["cluster"], data["name"]) == ("any-cluster", "team-a")
    expected = {"team": "a", "tier": "silver", "env": "prod", "kubernetes.io/metadata.name": "team-a"}
    assert data["labels"] == expected
    assert _read(client, headers, "team-a")["labels"] == expected


def test_removing_a_label_that_is_not_there_succeeds(client, headers):
    _create(client, headers, {"name": "team-a", "labels": {"team": "a"}})

    resp = _patch(client, headers, "team-a", {"remove": ["never-there"]})

    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["labels"]["team"] == "a"


def test_an_update_stamps_last_modified_and_leaves_created_alone(client, headers):
    """ADR 0002: created-* is written once; last-modified-* moves per update."""
    _create(client, headers, {"name": "team-a"})
    created = _read(client, headers, "team-a")["annotations"]

    _patch(client, headers, "team-a", {"set": {"team": "a"}}, request_id="req-patch")

    annotations = _read(client, headers, "team-a")["annotations"]
    assert annotations["cluster-service.test/created-by"] == created["cluster-service.test/created-by"]
    assert annotations["cluster-service.test/created-at"] == created["cluster-service.test/created-at"]
    assert annotations["cluster-service.test/last-modified-by"] == "test_admin"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", annotations["cluster-service.test/last-modified-at"])
    assert annotations["cluster-service.test/last-request-id"] == "req-patch"


def test_a_namespace_the_service_did_not_create_can_be_relabelled(client, headers):
    resp = _patch(client, headers, "dry-run-apps", {"set": {"team": "apps"}})

    assert resp.status_code == 200, resp.text
    annotations = _read(client, headers, "dry-run-apps")["annotations"]
    assert annotations["cluster-service.test/last-modified-by"] == "test_admin"
    assert "cluster-service.test/created-by" not in annotations
    # Everything else on it is left as it was.
    assert annotations["meta.helm.sh/release-name"] == "dry-run-release"


def test_relabelling_a_missing_namespace_is_404(client, headers):
    resp = _patch(client, headers, "ghost", {"set": {"a": "b"}})

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NAMESPACE_NOT_FOUND"


@pytest.mark.parametrize("name", ["default", "kube-system"])
def test_a_reserved_namespace_cannot_be_relabelled(client, headers, name):
    before = _read(client, headers, name)["labels"]

    resp = _patch(client, headers, name, {"set": {"team": "a"}})

    assert resp.status_code == 422
    assert _read(client, headers, name)["labels"] == before


@pytest.mark.parametrize("field", ["set", "remove"])
def test_reserved_label_keys_cannot_be_set_or_removed(client, headers, field):
    _create(client, headers, {"name": "team-a"})
    keys = ["pod-security.kubernetes.io/enforce", "kubernetes.io/metadata.name"]
    body = {"set": dict.fromkeys(keys, "x")} if field == "set" else {"remove": keys}

    resp = _patch(client, headers, "team-a", body)

    assert resp.status_code == 422
    for key in keys:
        assert key in resp.text
    assert _read(client, headers, "team-a")["labels"] == {"kubernetes.io/metadata.name": "team-a"}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"set": {}, "remove": []},
        {"set": {"team": "a"}, "remove": ["team"]},
        {"set": {"Bad Key": "x"}},
        {"remove": ["-bad"]},
        {"set": {"team": "has space"}},
        {"set": {"team": "a"}, "replace": True},
    ],
    ids=["empty", "both-empty", "same-key-both", "bad-key", "bad-remove-key", "bad-value", "unknown-field"],
)
def test_a_malformed_update_is_422_and_changes_nothing(client, headers, body):
    _create(client, headers, {"name": "team-a"})
    before = _read(client, headers, "team-a")

    assert _patch(client, headers, "team-a", body).status_code == 422
    assert _read(client, headers, "team-a") == before
