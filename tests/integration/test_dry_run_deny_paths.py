"""
tests/integration/test_dry_run_deny_paths.py

T11: dry-run weakens nothing.

A suite that only checks for 200s against a dry-run instance proves nothing,
because a dry-run that skipped auth, validation and every business rule would
pass it as well. This file collects the refusals in one place, across every
router, with Kubernetes and deploy-service both stubbed:

- auth: no token → 401; a valid token missing ``cluster_api`` / ``deploy_api``
  / ``command_api`` → 403
- validation: a bad body → 422, and an oversized batch → 422 *before any work*
- business rules: uncordon of a NotReady node → 409 with no way to override
  it, and a blocked drain → 400 having touched nothing
- contract: the whole OpenAPI document, the ``dry_run`` marker, the
  ``X-Coordination-ID`` → ``request_id`` round trip, and the error envelope

Some of these overlap the per-ticket files (``test_dry_run_scaffolding.py``,
``test_dry_run_node_routes.py``, ``test_dry_run_proxy_routes.py``) on purpose:
each of those proves its own seam, and this file proves the combined surface
from the outside, as an e2e suite would see it.

No ``e2e`` marker: none of this needs a cluster or an upstream.

Design shared with deploy-service; see that repo's docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.clients.dry_run_command_service_client import reset_dry_run_command_service
from app.clients.dry_run_deploy_service_client import reset_dry_run_deploy_service
from app.core.config import get_settings
from app.services.dry_run_kube_client import reset_dry_run_clusters

_READY = "dry-run-node-ready"
_NOT_READY = "dry-run-node-notready"
_UNKNOWN = "dry-run-node-unknown"
_CLUSTER = "/api/v1/clusters/any-cluster"
_NODES = f"{_CLUSTER}/nodes"
_COORD = "X-Coordination-ID"


def _reset() -> None:
    reset_dry_run_clusters()
    reset_dry_run_deploy_service()
    reset_dry_run_command_service()


def _client(monkeypatch, dry_run: bool) -> TestClient:
    monkeypatch.setenv("DRY_RUN_MODE", "true" if dry_run else "false")
    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app())


@pytest.fixture
def dry(monkeypatch) -> TestClient:
    _reset()
    with _client(monkeypatch, dry_run=True) as c:
        yield c
    _reset()
    get_settings.cache_clear()


@pytest.fixture
def live(monkeypatch) -> TestClient:
    with _client(monkeypatch, dry_run=False) as c:
        yield c
    get_settings.cache_clear()


def _token(*scopes: str) -> dict[str, str]:
    """Mint a token with exactly *scopes*.

    The fixture users cannot express "everything except cluster_api" (both
    hold it) or hold ``deploy_api`` at all, and the scope check reads only the
    token's claims, so minting is the precise way to withhold one scope.
    """
    from app.core.security import create_access_token

    token = create_access_token({"sub": "dry-run-deny", "scopes": list(scopes)})
    return {"Authorization": f"Bearer {token}"}


_ALL = ("cluster_api", "deploy_api", "command_api", "inventory_api")


def _call(client: TestClient, method: str, path: str, **kwargs):
    """``client.get`` takes no ``json``; ``request`` does for every verb."""
    return client.request(method.upper(), path, **kwargs)


def _all_but(scope: str) -> dict[str, str]:
    return _token(*(s for s in _ALL if s != scope))


@pytest.fixture
def full() -> dict[str, str]:
    return _token(*_ALL)


# ── 401: no token ─────────────────────────────────────────────────────────────

# One route per router, reads and writes both, so a dry-run branch that skipped
# auth on any of them shows up here.
_PROTECTED = [
    ("get", "/api/v1/clusters"),
    ("get", f"{_NODES}/{_READY}"),
    ("post", f"{_NODES}/{_READY}/cordon"),
    ("post", f"{_NODES}/{_READY}/drain"),
    ("post", f"{_CLUSTER}/nodes:uncordon"),
    ("get", f"{_CLUSTER}/pods"),
    ("get", f"{_CLUSTER}/configmaps"),
    ("post", "/api/v1/deploy"),
    ("get", "/api/v1/deploy/1"),
    ("get", "/api/v1/command/info"),
    ("post", "/api/v1/command/execution"),
    ("get", "/api/v1/command/execution/x/trace/ui"),
    ("get", f"/api/v1/inventory/nodes/{_READY}"),
]


@pytest.mark.parametrize("method, path", _PROTECTED)
def test_no_token_is_401(dry, method, path):
    assert getattr(dry, method)(path).status_code == 401


@pytest.mark.parametrize("method, path", _PROTECTED)
def test_garbage_token_is_401(dry, method, path):
    resp = getattr(dry, method)(path, headers={"Authorization": "Bearer nonsense"})
    assert resp.status_code == 401


# ── 403: valid token, one scope withheld ──────────────────────────────────────


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/v1/clusters"),
        ("get", f"{_NODES}/{_READY}"),
        ("post", f"{_NODES}/{_READY}/cordon"),
        ("post", f"{_NODES}/{_READY}/uncordon"),
        ("post", f"{_NODES}/{_READY}/drain"),
        ("post", f"{_CLUSTER}/nodes:cordon"),
        ("get", f"{_CLUSTER}/configmaps?namespace=*"),
    ],
)
def test_missing_cluster_api_is_403(dry, method, path):
    resp = _call(dry, method, path, headers=_all_but("cluster_api"), json={"nodes": [_READY]})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "FORBIDDEN"


def test_a_403_cordon_changed_nothing(dry, full):
    """The scope check runs before the handler, so the fake node is untouched."""
    dry.post(f"{_NODES}/{_READY}/cordon", headers=_all_but("cluster_api"))
    node = dry.get(f"{_NODES}/{_READY}", headers=full).json()["data"]
    assert node["unschedulable"] is False


@pytest.mark.parametrize(
    "method, path",
    [
        ("post", "/api/v1/deploy?action=a"),
        ("post", "/api/v1/deploy/check-running?action=a"),
        ("get", "/api/v1/deploy/1"),
        ("post", "/api/v1/deploy/1/cancel"),
        ("post", "/api/v1/deploy/1/retry"),
    ],
)
def test_missing_deploy_api_is_403(dry, method, path):
    resp = getattr(dry, method)(path, headers=_all_but("deploy_api"))
    assert resp.status_code == 403


def test_a_403_trigger_created_no_pipeline(dry, full):
    dry.post("/api/v1/deploy?action=a", headers=_all_but("deploy_api"))
    running = dry.post("/api/v1/deploy/check-running?action=a", headers=full)
    assert running.json()["data"]["count"] == 0


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/v1/command/info"),
        ("get", "/api/v1/command/dry_run_ansible_ping/info"),
        ("post", "/api/v1/command/execution"),
        ("get", "/api/v1/command/execution/x"),
        ("get", "/api/v1/command/execution/x/trace/ui"),
        ("post", "/api/v1/command/execution/x/kill"),
    ],
)
def test_missing_command_api_is_403(dry, method, path):
    resp = _call(
        dry, method, path,
        headers=_all_but("command_api"),
        json={"command_name": "dry_run_ansible_ping", "host": "10.0.0.1", "username": "u"},
    )
    assert resp.status_code == 403


# ── 422: validation ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("patch", f"{_NODES}/{_READY}/labels", {"set": "not-a-map"}),
        ("patch", f"{_NODES}/{_READY}/annotations", {"remove": "not-a-list"}),
        ("post", f"{_NODES}/{_READY}/drain", {"options": {"force": "not-a-bool"}}),
        ("post", f"{_CLUSTER}/nodes:cordon", {"nodes": "not-a-list"}),
        ("post", f"{_CLUSTER}/nodes:uncordon", {}),
        ("post", "/api/v1/deploy?action=a", {"variables": "not-a-list"}),
        ("post", "/api/v1/command/execution", {"command_name": "dry_run_ansible_ping"}),
    ],
)
def test_invalid_body_is_422(dry, full, method, path, body):
    assert getattr(dry, method)(path, headers=full, json=body).status_code == 422


@pytest.mark.parametrize("action", ["cordon", "uncordon"])
def test_oversized_batch_is_422_before_any_work(dry, full, action):
    """101 names, the first a real node in a state the action would change.

    Cordon a ready node / uncordon a cordoned one: if any per-node work had
    started before the cap was enforced, the first node's state would differ
    afterwards. It must not.
    """
    if action == "uncordon":
        dry.post(f"{_NODES}/{_READY}/cordon", headers=full)
    before = dry.get(f"{_NODES}/{_READY}", headers=full).json()["data"]["unschedulable"]

    resp = dry.post(
        f"{_CLUSTER}/nodes:{action}",
        headers=full,
        json={"nodes": [_READY] + [f"node-{i}" for i in range(100)]},
    )
    assert resp.status_code == 422

    after = dry.get(f"{_NODES}/{_READY}", headers=full).json()["data"]["unschedulable"]
    assert after == before


def test_a_batch_of_exactly_100_is_accepted(dry, full):
    """The boundary, so the 422 above is the cap and not something else."""
    resp = dry.post(
        f"{_CLUSTER}/nodes:cordon",
        headers=full,
        json={"nodes": [_READY] + [f"node-{i}" for i in range(99)]},
    )
    assert resp.status_code == 200


# ── 409: uncordon a NotReady node, no override ────────────────────────────────


@pytest.mark.parametrize("node", [_NOT_READY, _UNKNOWN])
@pytest.mark.parametrize(
    "params, body",
    [
        (None, None),
        # Every plausible spelling of an override. None exists, so each is
        # ignored by the route rather than honoured.
        ({"force": "true"}, None),
        ({"ignore_readiness": "true", "skip_ready_check": "true"}, None),
        (None, {"force": True, "ignore_readiness": True}),
    ],
)
def test_uncordon_of_an_unhealthy_node_cannot_be_overridden(dry, full, node, params, body):
    dry.post(f"{_NODES}/{node}/cordon", headers=full)

    resp = dry.post(f"{_NODES}/{node}/uncordon", headers=full, params=params, json=body)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NODE_NOT_READY"

    still = dry.get(f"{_NODES}/{node}", headers=full).json()["data"]
    assert still["unschedulable"] is True, "the refusal must leave the cordon in place"


def test_batch_uncordon_refuses_unhealthy_nodes_per_node(dry, full):
    resp = dry.post(
        f"{_CLUSTER}/nodes:uncordon",
        headers=full,
        params={"force": "true"},
        json={"nodes": [_READY, _NOT_READY, _UNKNOWN]},
    )
    assert resp.status_code == 200
    outcomes = {r["node"]: r for r in resp.json()["data"]["results"]}
    assert outcomes[_READY]["status"] == "success"
    for node in (_NOT_READY, _UNKNOWN):
        assert outcomes[node]["status"] == "failed"
        assert outcomes[node]["error_code"] == "NODE_NOT_READY"


def test_the_contract_offers_no_uncordon_override(dry):
    """The strongest form of "no override": the published contract has no
    parameter or body that could carry one. If someone adds one, this fails
    and the change has to be argued for, not slipped in."""
    paths = dry.get("/openapi.json").json()["paths"]
    single = paths["/api/v1/clusters/{cluster}/nodes/{node}/uncordon"]["post"]
    assert {p["name"] for p in single.get("parameters", [])} == {"cluster", "node"}
    assert "requestBody" not in single

    batch = paths["/api/v1/clusters/{cluster}/nodes:uncordon"]["post"]
    assert {p["name"] for p in batch.get("parameters", [])} == {"cluster"}


# ── 400: drain blocked, nothing touched ───────────────────────────────────────


def _pods_on(client, headers, node) -> set[str]:
    resp = client.get(
        f"{_CLUSTER}/pods", headers=headers, params={"namespace": "*", "node": node}
    )
    return {p["name"] for p in resp.json()["data"]["pods"]}


@pytest.mark.parametrize(
    "options, still_blocking",
    [
        ({}, {"dry-run-unmanaged", "dry-run-emptydir"}),
        ({"options": {"force": True}}, {"dry-run-emptydir"}),
        ({"options": {"delete_emptydir_data": True}}, {"dry-run-unmanaged"}),
    ],
)
def test_blocked_drain_is_400_and_touches_nothing(dry, full, options, still_blocking):
    before = _pods_on(dry, full, _READY)

    resp = dry.post(f"{_NODES}/{_READY}/drain", headers=full, json=options)
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["code"] == "DRAIN_BLOCKED"
    named = str(body.get("detail"))
    for pod in still_blocking:
        assert pod in named, f"{pod} not named in the refusal"

    assert _pods_on(dry, full, _READY) == before, "a blocked drain evicted something"
    # The cordon is the one thing a refusal keeps, deliberately: it is what the
    # caller asked for and a corrected retry then has nothing to redo. See
    # docs/adr/0001-*. "Touched nothing" means no pod, not no cordon.
    node = dry.get(f"{_NODES}/{_READY}", headers=full).json()["data"]
    assert node["unschedulable"] is True


# ── contract ──────────────────────────────────────────────────────────────────


def test_openapi_document_is_identical_to_production(dry, live):
    """The whole document, not just paths and schemas: parameters, security,
    descriptions and status codes all have to match for a caller written
    against dry-run to be written against the real contract."""
    assert dry.get("/openapi.json").json() == live.get("/openapi.json").json()


@pytest.mark.parametrize(
    "path, scopes",
    [
        ("/api/v1/clusters", ("cluster_api",)),
        (f"{_NODES}/{_READY}", ("cluster_api",)),
        ("/api/v1/command/info", ("command_api",)),
        (f"/api/v1/inventory/nodes/{_READY}", ("inventory_api",)),
    ],
)
def test_marker_is_true_on_every_router_in_dry_run(dry, path, scopes):
    assert dry.get(path, headers=_token(*scopes)).json()["dry_run"] is True


def test_marker_is_true_on_a_write_in_dry_run(dry, full):
    assert dry.post("/api/v1/deploy?action=a", headers=full).json()["dry_run"] is True


def test_marker_is_false_when_off(live):
    resp = live.get("/api/v1/auth/my-scopes", headers=_token("cluster_api"))
    assert resp.json()["dry_run"] is False


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", f"{_NODES}/{_READY}"),            # success, Kubernetes side
        ("post", "/api/v1/deploy?action=a"),      # success, deploy-service side
        ("post", f"{_NODES}/{_NOT_READY}/uncordon"),  # business-rule error
        ("get", "/api/v1/deploy/1"),              # upstream-error adapter
        ("get", "/api/v1/command/info"),          # app 403 (no command scope below)
    ],
)
def test_coordination_id_round_trips(dry, method, path):
    headers = {**_token("cluster_api", "deploy_api"), _COORD: "dry-run-coord-123"}
    resp = getattr(dry, method)(path, headers=headers)
    assert resp.headers[_COORD] == "dry-run-coord-123"
    assert resp.json()["request_id"] == "dry-run-coord-123"


def test_no_coordination_id_means_a_generated_request_id_and_no_echo(dry, full):
    resp = dry.get(f"{_NODES}/{_READY}", headers=full)
    assert resp.json()["request_id"]
    assert _COORD not in resp.headers


@pytest.mark.parametrize(
    "method, path",
    [
        ("post", f"{_NODES}/{_NOT_READY}/uncordon"),   # 409 from NodeService
        ("post", f"{_NODES}/{_READY}/drain"),          # 400 DRAIN_BLOCKED
        ("get", f"{_NODES}/no-such-node"),             # 404 via the fake's ApiException
        ("get", "/api/v1/deploy/1"),                   # 502 via DeployServiceError
        ("get", "/api/v1/inventory/nodes/no-such-node"),  # 404 via inventory translation
    ],
)
def test_error_envelope_is_unchanged(dry, full, method, path):
    body = _call(dry, method, path, headers=full, json={}).json()
    assert set(body) == {"error", "request_id"}, "errors carry no dry_run marker"
    assert {"code", "message"} <= set(body["error"])
    assert set(body["error"]) <= {"code", "message", "detail"}
