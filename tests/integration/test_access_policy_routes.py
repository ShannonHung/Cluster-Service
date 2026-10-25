"""
tests/integration/test_access_policy_routes.py

The access policy in USERS_JSON_PATH, end to end through the HTTP surface.

Runs under DRY_RUN_MODE so no cluster is needed — which doubles as the proof
that the policy sits above the dry-run seam: a refused request is refused in
dry-run exactly as in production.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.dependencies import get_cluster_repo, get_policy_service
from app.core.exceptions import UsersFileInvalidException
from app.services.dry_run_kube_client import reset_dry_run_clusters
from tests.integration.test_kube_routes_off_event_loop import _ROUTES as _KUBE_ROUTES

# Deliberately not a bcrypt hash — the input the validator must refuse.
# Named rather than inlined next to the password key, so secret scanners
# do not read a test input as a leaked credential.
_NOT_A_HASH = "plain-text-value"
_FIXTURE = Path(__file__).parent.parent / "fixtures" / "users.json"
_READY = "dry-run-node-ready"
_OTHER = "dry-run-node-notready"

_POLICY = {
    "policies": [
        {
            "id": "no-prod",
            "description": "prod is maintained by the SRE pipeline only",
            "actions": ["node.*", "namespace.*"],
            "effect": "deny",
            "match": {"cluster": "prod-.*"},
        },
        {
            "id": "keep-ready-node",
            "actions": ["node.drain", "node.cordon"],
            "effect": "deny",
            "match": {"cluster": "dev-1", "node": _READY},
        },
    ],
    "users": None,  # filled from the fixture
    "_operator_rules": [
        {
            "id": "operator-dev-only",
            "actions": ["node.*", "namespace.*"],
            "effect": "allow",
            "match": {"cluster": "dev-.*"},
        },
    ],
}


def _users_file(tmp_path: Path) -> Path:
    users = json.loads(_FIXTURE.read_text())
    for u in users:
        if u["account"] == "test_operator":
            u["policies"] = _POLICY["_operator_rules"]
    path = tmp_path / "users.json"
    path.write_text(json.dumps({"policies": _POLICY["policies"], "users": users}))
    return path


@pytest.fixture
def client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("USERS_JSON_PATH", str(_users_file(tmp_path)))
    get_settings.cache_clear()
    reset_dry_run_clusters()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    reset_dry_run_clusters()
    get_settings.cache_clear()


def _auth(client: TestClient, account: str) -> dict[str, str]:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def admin(client):
    return _auth(client, "test_admin")


@pytest.fixture
def operator(client):
    return _auth(client, "test_operator")


def _assert_denied(resp, rule_id):
    assert resp.status_code == 403, resp.text
    error = resp.json()["error"]
    assert error["code"] == "POLICY_DENIED"
    assert error["detail"]["rule_id"] == rule_id
    return error


# ── single-resource writes ────────────────────────────────────────────────────


def test_global_deny_refuses_drain_on_a_prod_cluster(client, admin):
    resp = client.post(f"/api/v1/clusters/prod-east/nodes/{_READY}/drain", headers=admin)
    error = _assert_denied(resp, "no-prod")
    assert "SRE pipeline" in error["message"]
    assert error["detail"]["layer"] == "global"


def test_a_request_level_drain_dry_run_is_refused_too(client, admin):
    """dry_run is a preview of the real drain — it must preview the refusal."""
    resp = client.post(
        f"/api/v1/clusters/prod-east/nodes/{_READY}/drain",
        json={"dry_run": True}, headers=admin,
    )
    _assert_denied(resp, "no-prod")


def test_node_regex_refuses_one_node_and_not_another(client, admin):
    _assert_denied(
        client.post(f"/api/v1/clusters/dev-1/nodes/{_READY}/cordon", headers=admin),
        "keep-ready-node",
    )
    ok = client.post(f"/api/v1/clusters/dev-1/nodes/{_OTHER}/cordon", headers=admin)
    assert ok.status_code == 200, ok.text


def test_a_refused_write_changes_nothing(client, admin):
    client.post(f"/api/v1/clusters/dev-1/nodes/{_READY}/cordon", headers=admin)
    node = client.get(f"/api/v1/clusters/dev-1/nodes/{_READY}", headers=admin).json()["data"]
    assert node["unschedulable"] is False


def test_reads_are_not_policed(client, admin):
    resp = client.get(f"/api/v1/clusters/prod-east/nodes/{_READY}", headers=admin)
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("post", f"nodes/{_READY}/uncordon", None),
        ("patch", f"nodes/{_READY}/labels", {"set": {"a": "b"}}),
        ("patch", f"nodes/{_READY}/annotations", {"set": {"a": "b"}}),
        ("patch", f"nodes/{_READY}/taints", {"remove": [{"key": "a", "effect": "NoSchedule"}]}),
        ("post", "namespaces", {"name": "team-a"}),
        ("patch", "namespaces/team-a/labels", {"set": {"a": "b"}}),
    ],
)
def test_every_write_route_is_policed(client, admin, method, path, body):
    resp = client.request(method, f"/api/v1/clusters/prod-east/{path}", json=body, headers=admin)
    _assert_denied(resp, "no-prod")


# ── user layer ────────────────────────────────────────────────────────────────


def test_user_allow_list_admits_its_clusters(client, operator):
    resp = client.post(f"/api/v1/clusters/dev-2/nodes/{_OTHER}/cordon", headers=operator)
    assert resp.status_code == 200, resp.text


def test_user_allow_list_refuses_other_clusters(client, operator):
    resp = client.post(f"/api/v1/clusters/stg-1/nodes/{_OTHER}/cordon", headers=operator)
    error = _assert_denied(resp, None)
    assert error["detail"]["layer"] == "user"
    assert "operator-dev-only" in error["message"]


def test_the_allow_list_is_per_user(client, admin):
    resp = client.post(f"/api/v1/clusters/stg-1/nodes/{_OTHER}/cordon", headers=admin)
    assert resp.status_code == 200, resp.text


# ── batches ───────────────────────────────────────────────────────────────────


def test_batch_reports_denied_nodes_and_runs_the_rest(client, admin):
    resp = client.post(
        "/api/v1/clusters/dev-1/nodes:cordon",
        json={"nodes": [_READY, _OTHER, "no-such-node"]}, headers=admin,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert [(r["node"], r["status"]) for r in data["results"]] == [
        (_READY, "failed"),
        (_OTHER, "success"),
        ("no-such-node", "failed"),
    ]
    denied = data["results"][0]
    assert denied["error_code"] == "POLICY_DENIED"
    assert "keep-ready-node" in denied["message"]
    assert denied["kube_status"] is None
    assert data["results"][2]["error_code"] == "NODE_NOT_FOUND"
    assert data["summary"] == {"total": 3, "succeeded": 1, "failed": 2}

    node = client.get(f"/api/v1/clusters/dev-1/nodes/{_READY}", headers=admin).json()["data"]
    assert node["unschedulable"] is False


def test_a_fully_denied_batch_is_still_200(client, admin):
    resp = client.post(
        "/api/v1/clusters/prod-east/nodes:uncordon",
        json={"nodes": [_READY, _OTHER]}, headers=admin,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["action"] == "uncordon"
    assert data["summary"] == {"total": 2, "succeeded": 0, "failed": 2}
    assert {r["error_code"] for r in data["results"]} == {"POLICY_DENIED"}


# ── broken policy fails closed ────────────────────────────────────────────────


def test_a_broken_edit_refuses_writes_until_fixed(client, admin):
    path = Path(get_settings().USERS_JSON_PATH)
    good = path.read_text()
    doc = json.loads(good)
    doc["policies"][0]["match"] = {"cluster": "prod-("}
    path.write_text(json.dumps(doc))

    resp = client.post(f"/api/v1/clusters/dev-1/nodes/{_OTHER}/cordon", headers=admin)
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "USERS_FILE_INVALID"
    assert str(path) not in resp.text

    path.write_text(good + " ")  # new size, so the cache sees a change
    resp = client.post(f"/api/v1/clusters/dev-1/nodes/{_OTHER}/cordon", headers=admin)
    assert resp.status_code == 200, resp.text


@pytest.mark.parametrize(
    "content",
    [
        {"policies": [{"id": "x"}], "users": []},
        [{"account": "alice", "hashed_password": _NOT_A_HASH, "scopes": []}],
    ],
    ids=["bad-rule", "bad-user"],
)
def test_startup_refuses_an_invalid_users_file(monkeypatch, tmp_path, content):
    path = tmp_path / "users.json"
    path.write_text(json.dumps(content))
    monkeypatch.setenv("USERS_JSON_PATH", str(path))
    get_settings.cache_clear()
    try:
        from app.main import create_app

        with pytest.raises(UsersFileInvalidException):
            with TestClient(create_app()):
                pass
    finally:
        get_settings.cache_clear()


# ── guard ─────────────────────────────────────────────────────────────────────


def _depends_on(dependant, target) -> bool:
    return any(d.call is target or _depends_on(d, target) for d in dependant.dependencies)


def test_every_kubernetes_write_route_checks_the_policy(client):
    """A new write route on a cluster cannot ship without a policy check."""
    unpoliced = [
        f"{method} {route.path}"
        for route in client.app.routes
        if isinstance(route, APIRoute) and _depends_on(route.dependant, get_cluster_repo)
        for method in route.methods
        if method != "GET" and not _depends_on(route.dependant, get_policy_service)
    ]
    assert unpoliced == []


class _UntouchableRepo:
    """Any use means the route reached for the cluster before the policy."""

    def __getattr__(self, name):
        raise AssertionError(f"cluster repository used ({name}) despite a deny-all policy")


# Every non-GET route that reaches a cluster. The table is kept complete by
# test_every_route_reaching_a_cluster_is_tabled in that module.
_WRITES = [r for r in _KUBE_ROUTES if r[1] != "get"]


@pytest.mark.parametrize(
    "method, path, params, body",
    [r[1:] for r in _WRITES],
    ids=[f"{m.upper()} {p.split('any-cluster')[-1]}" for _, m, p, _, _ in _WRITES],
)
def test_every_write_is_refused_before_the_cluster_is_touched(
    monkeypatch, tmp_path, method, path, params, body,
):
    """Behavioural, not structural: injecting the policy is not enough — the
    route must call it, and before call_kube."""
    users = json.loads(_FIXTURE.read_text())
    deny_all = {"id": "deny-all", "actions": ["*"], "effect": "deny", "match": {"cluster": ".*"}}
    users_path = tmp_path / "users.json"
    users_path.write_text(json.dumps({"policies": [deny_all], "users": users}))
    monkeypatch.setenv("USERS_JSON_PATH", str(users_path))
    get_settings.cache_clear()
    from app.main import create_app

    app = create_app()
    app.dependency_overrides[get_cluster_repo] = _UntouchableRepo
    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.request(
                method.upper(), path, params=params, json=body,
                headers=_auth(c, "test_admin"),
            )
    finally:
        get_settings.cache_clear()

    if ":" in path.rsplit("/", 1)[-1]:  # batch: always 200, every node denied
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["summary"]["failed"] == data["summary"]["total"] > 0
        assert {r["error_code"] for r in data["results"]} == {"POLICY_DENIED"}
    else:
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "POLICY_DENIED"
