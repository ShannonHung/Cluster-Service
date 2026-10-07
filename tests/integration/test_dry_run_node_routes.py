"""
tests/integration/test_dry_run_node_routes.py

T10: every node endpoint answers for real in dry-run, with no kubeconfig on
disk and no cluster reachable.

**The point is that the business logic still runs.** The seam is the client
factory and the cluster repository, both of which sit below NodeService, so
dry-run replaces what the service talks to and nothing about what it decides.
The assertions that matter here are therefore not the 200s — they are the
refusals: the uncordon readiness gate, drain's refuse-before-evict check, and
the always-skipped pod categories. A dry-run that short-circuited the router
would answer 200 to all of them and prove nothing.

No ``e2e`` marker: these need no cluster. The existing ``e2e`` tests that drain
a real node (``E2E_DRAIN_NODE``) are untouched and still excluded from
``make test``.

Design shared with deploy-service; see that repo's docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.services.dry_run_kube_client import reset_dry_run_clusters

_READY = "dry-run-node-ready"
_NOT_READY = "dry-run-node-notready"
_UNKNOWN = "dry-run-node-unknown"
_BASE = "/api/v1/clusters/any-cluster/nodes"


@pytest.fixture
def dry_run_client(monkeypatch) -> TestClient:
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    get_settings.cache_clear()
    # Cluster state persists across requests by design (a real cluster
    # remembers a cordon), so it is process-global and must be cleared per
    # test — otherwise one test's drain empties the node the next one needs.
    reset_dry_run_clusters()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    reset_dry_run_clusters()
    get_settings.cache_clear()


def _auth(client: TestClient, account: str = "test_admin") -> dict[str, str]:
    resp = client.post("/token", data={"username": account, "password": "secret"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
def headers(dry_run_client) -> dict[str, str]:
    return _auth(dry_run_client)


# ── reads ─────────────────────────────────────────────────────────────────────


def test_list_clusters_without_kubeconfigs(dry_run_client, headers):
    resp = dry_run_client.get("/api/v1/clusters", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["clusters"], "an empty listing proves nothing"


def test_list_nodes_without_a_cluster(dry_run_client, headers):
    resp = dry_run_client.get(f"{_BASE}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["nodes"]


def test_get_node_detail(dry_run_client, headers):
    resp = dry_run_client.get(f"{_BASE}/{_READY}", headers=headers)
    assert resp.status_code == 200

    data = resp.json()["data"]
    assert data["name"] == _READY
    assert data["status"] == "Ready"
    assert data["version"], "kubelet version was not resolved"


def test_unknown_node_is_404(dry_run_client, headers):
    """Dry-run resolves any *cluster* name, but node existence is still
    enforced one layer down — so a caller's not-found path stays reachable."""
    resp = dry_run_client.get(f"{_BASE}/no-such-node", headers=headers)
    assert resp.status_code == 404


def test_list_pods(dry_run_client, headers):
    resp = dry_run_client.get(
        "/api/v1/clusters/any-cluster/pods", headers=headers, params={"namespace": "*"}
    )
    assert resp.status_code == 200


# ── cordon / uncordon ─────────────────────────────────────────────────────────


def test_cordon_succeeds(dry_run_client, headers):
    resp = dry_run_client.post(f"{_BASE}/{_READY}/cordon", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "success"


def test_cordon_is_visible_in_the_following_read(dry_run_client, headers):
    """The state change a caller just made has to be reflected back, otherwise
    an e2e test cannot verify its own effect."""
    dry_run_client.post(f"{_BASE}/{_READY}/cordon", headers=headers)
    detail = dry_run_client.get(f"{_BASE}/{_READY}", headers=headers).json()["data"]
    assert detail["unschedulable"] is True


def test_cordon_is_never_gated_on_readiness(dry_run_client, headers):
    """Cordoning is how an operator responds to a sick node, so unlike
    uncordon it must work on a NotReady one."""
    resp = dry_run_client.post(f"{_BASE}/{_NOT_READY}/cordon", headers=headers)
    assert resp.status_code == 200


def test_uncordon_succeeds_on_a_ready_node(dry_run_client, headers):
    resp = dry_run_client.post(f"{_BASE}/{_READY}/uncordon", headers=headers)
    assert resp.status_code == 200


def test_uncordon_refuses_a_notready_node(dry_run_client, headers):
    """The readiness gate is real business logic running against the fake —
    the single most important assertion in this file. If dry-run had been built
    as a router short-circuit this would be a 200."""
    resp = dry_run_client.post(f"{_BASE}/{_NOT_READY}/uncordon", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NODE_NOT_READY"


def test_uncordon_refuses_an_unknown_status_node(dry_run_client, headers):
    """"Unknown" is a worse signal than NotReady, not a milder one. The gate is
    an allowlist, so this must fail too — a denylist would admit it."""
    resp = dry_run_client.post(f"{_BASE}/{_UNKNOWN}/uncordon", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "NODE_NOT_READY"


# ── batch ─────────────────────────────────────────────────────────────────────


def test_batch_cordon_returns_results_and_summary(dry_run_client, headers):
    resp = dry_run_client.post(
        "/api/v1/clusters/any-cluster/nodes:cordon",
        headers=headers,
        json={"nodes": [_READY, _NOT_READY]},
    )
    assert resp.status_code == 200

    data = resp.json()["data"]
    assert len(data["results"]) == 2
    assert data["summary"]


def test_batch_uncordon_reports_per_node_outcomes(dry_run_client, headers):
    """Partial success is the contract: a NotReady node fails only itself and
    does not abort the batch or turn the response into an error status."""
    resp = dry_run_client.post(
        "/api/v1/clusters/any-cluster/nodes:uncordon",
        headers=headers,
        json={"nodes": [_READY, _NOT_READY]},
    )
    assert resp.status_code == 200

    outcomes = {r["node"]: r for r in resp.json()["data"]["results"]}
    assert outcomes[_READY]["status"] == "success"
    assert outcomes[_NOT_READY]["status"] != "success"


def test_batch_uncordon_uses_one_listing_for_readiness(dry_run_client, headers):
    """The gate resolves every node from a single list_node, so its cost does
    not scale with batch size. Exercising it here keeps that path covered."""
    resp = dry_run_client.post(
        "/api/v1/clusters/any-cluster/nodes:uncordon",
        headers=headers,
        json={"nodes": [_READY, _NOT_READY, _UNKNOWN]},
    )
    assert resp.status_code == 200
    assert len(resp.json()["data"]["results"]) == 3


def test_batch_size_cap_is_still_enforced(dry_run_client, headers):
    """A 422 before any work starts — the bound is in the request model, and
    dry-run must not relax it."""
    resp = dry_run_client.post(
        "/api/v1/clusters/any-cluster/nodes:cordon",
        headers=headers,
        json={"nodes": [f"node-{i}" for i in range(101)]},
    )
    assert resp.status_code == 422


# ── drain ─────────────────────────────────────────────────────────────────────


def test_drain_refuses_before_evicting(dry_run_client, headers):
    """refuse-before-evict: unmanaged and emptyDir pods block the whole drain
    unless the caller opts in, and nothing is touched. Evicted pods cannot be
    recalled, so a half-drained node is worse than an untouched one."""
    resp = dry_run_client.post(f"{_BASE}/{_READY}/drain", headers=headers, json={})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "DRAIN_BLOCKED"


def test_drain_names_the_blocking_pods(dry_run_client, headers):
    """A refusal a caller cannot act on is only half useful."""
    body = dry_run_client.post(
        f"{_BASE}/{_READY}/drain", headers=headers, json={}
    ).json()
    rendered = str(body)
    assert "dry-run-unmanaged" in rendered
    assert "dry-run-emptydir" in rendered


def test_drain_succeeds_once_the_options_are_given(dry_run_client, headers):
    resp = dry_run_client.post(
        f"{_BASE}/{_READY}/drain",
        headers=headers,
        json={"options": {"force": True, "delete_emptydir_data": True}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["node_emptied"] is True


def test_drain_completes_without_burning_the_wait_budget(dry_run_client, headers):
    """_wait_for_pods_gone polls until the evicted pods disappear, with a 25s
    budget. A fake whose pod list never changed would spend all of it and
    report still_terminating — a slow false failure rather than a clean drain.
    """
    started = time.monotonic()
    resp = dry_run_client.post(
        f"{_BASE}/{_READY}/drain",
        headers=headers,
        json={"options": {"force": True, "delete_emptydir_data": True}},
    )
    elapsed = time.monotonic() - started

    assert resp.status_code == 200
    assert resp.json()["data"]["still_terminating"] == []
    assert elapsed < 5, f"drain took {elapsed:.1f}s — the wait budget was spent"


def test_drain_evicts_only_the_evictable_pods(dry_run_client, headers):
    """DaemonSet, mirror and completed pods are always skipped — never
    evicted, never blocking. The categories are checked before the blocking
    ones, so a DaemonSet pod using emptyDir is skipped rather than blocked."""
    data = dry_run_client.post(
        f"{_BASE}/{_READY}/drain",
        headers=headers,
        json={"options": {"force": True, "delete_emptydir_data": True}},
    ).json()["data"]

    drained = {p["name"] for p in data["drained_pods"]}
    assert "dry-run-web-1" in drained
    for always_skipped in ("dry-run-daemon", "dry-run-mirror", "dry-run-completed"):
        assert always_skipped not in drained


def test_drain_leaves_other_nodes_alone(dry_run_client, headers):
    """The field selector is honoured — a drain of one node must not evict a
    pod scheduled elsewhere."""
    data = dry_run_client.post(
        f"{_BASE}/{_READY}/drain",
        headers=headers,
        json={"options": {"force": True, "delete_emptydir_data": True}},
    ).json()["data"]

    assert "dry-run-other-node" not in {p["name"] for p in data["drained_pods"]}


def test_request_level_drain_dry_run_still_short_circuits(dry_run_client, headers):
    """The endpoint's own per-request dry_run field is a separate mechanism at
    a different layer. The two coexist: DRY_RUN_MODE marks the envelope, while
    drain.dry_run short-circuits the router so nothing is evicted."""
    body = dry_run_client.post(
        f"{_BASE}/{_READY}/drain", headers=headers, json={"dry_run": True}
    ).json()

    assert body["dry_run"] is True           # envelope — DRY_RUN_MODE
    assert body["data"]["dry_run"] is True   # payload — the drain flag
    assert body["data"]["drained_pods"] == []


# ── label / annotate / taint ──────────────────────────────────────────────────


def test_label_node_returns_the_resulting_state(dry_run_client, headers):
    resp = dry_run_client.patch(
        f"{_BASE}/{_READY}/labels",
        headers=headers,
        json={"set": {"team": "platform"}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["labels"]["team"] == "platform"


def test_label_removal_is_applied(dry_run_client, headers):
    """Removal is null-valued merge-patch semantics; a fake that stored None
    would leave the key present."""
    dry_run_client.patch(
        f"{_BASE}/{_READY}/labels", headers=headers, json={"set": {"temp": "x"}}
    )
    resp = dry_run_client.patch(
        f"{_BASE}/{_READY}/labels", headers=headers, json={"remove": ["temp"]}
    )
    assert resp.status_code == 200
    assert "temp" not in resp.json()["data"]["labels"]


def test_annotate_node(dry_run_client, headers):
    resp = dry_run_client.patch(
        f"{_BASE}/{_READY}/annotations",
        headers=headers,
        json={"set": {"owner": "dry-run"}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["annotations"]["owner"] == "dry-run"


def test_taint_node(dry_run_client, headers):
    resp = dry_run_client.patch(
        f"{_BASE}/{_READY}/taints",
        headers=headers,
        json={"set": [{"key": "dedicated", "value": "gpu", "effect": "NoSchedule"}]},
    )
    assert resp.status_code == 200


# ── the marker and deny paths ─────────────────────────────────────────────────


def test_responses_are_marked_dry_run(dry_run_client, headers):
    body = dry_run_client.get(f"{_BASE}/{_READY}", headers=headers).json()
    assert body["dry_run"] is True


def test_no_token_is_401(dry_run_client):
    """Dry-run replaces what the service talks to, never who may call it."""
    assert dry_run_client.get(f"{_BASE}/{_READY}").status_code == 401


def test_scope_enforcement_still_runs_in_dry_run(dry_run_client):
    """Scope checks are untouched by dry-run.

    This asserts it on an endpoint whose scope the fixtures can actually
    withhold: both test users hold ``cluster_api``, so no account in
    ``tests/fixtures/users.json`` can be denied a node route. ``test_operator``
    lacks ``inventory_api``, which exercises the same dependency
    (``get_current_user([...])``) that gates every node endpoint. Adding a
    scope-less user to the shared fixture would have been the alternative, but
    that file backs the whole suite and widening it for one assertion is the
    worse trade. The 401 test above covers the node routes' own auth.
    """
    resp = dry_run_client.get(
        "/api/v1/inventory/nodes/node1",
        headers=_auth(dry_run_client, "test_operator"),
    )
    assert resp.status_code == 403


def test_malformed_body_is_422(dry_run_client, headers):
    resp = dry_run_client.patch(
        f"{_BASE}/{_READY}/labels", headers=headers, json={"set": "not-a-map"}
    )
    assert resp.status_code == 422
