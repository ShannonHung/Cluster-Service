"""
tests/integration/test_kube_routes_off_event_loop.py

Every Kubernetes route resolves its cluster through the one shared seam and
does its blocking work off the event loop (CLAUDE.md, "Never call a Kubernetes
service inline from a route").

Three things block, and all three are checked per route:

- resolving credentials — reads a kubeconfig, and may run an exec credential
  plugin (EKS / GKE) that takes seconds
- building the client — can write a CA temp file
- each Kubernetes call — the SDK blocks on urllib3 sockets

Any one of them on the event loop stalls every other request, health checks
included. Each is recorded from inside the collaborators themselves, so the
test fails if any of the three moves back.

The repository is swapped through ``get_cluster_repo`` alone. A route that
built its own repository — a private copy of the dry-run seam — would never
see the recording one, and fails here on an empty record.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.dependencies import get_cluster_repo
from app.repositories.dry_run_cluster_repository import DryRunClusterRepository
from app.services import kube_client
from app.services.dry_run_kube_client import reset_dry_run_clusters

_CLUSTER = "/api/v1/clusters/any-cluster"
_NODE = f"{_CLUSTER}/nodes/dry-run-node-ready"
_BATCH = {"nodes": ["dry-run-node-ready"]}

# (router module, method, path, query params, JSON body) — every route that
# talks to a cluster.
_ROUTES = [
    ("clusters", "get", f"{_CLUSTER}/nodes", {}, None),
    ("nodes", "get", _NODE, {}, None),
    ("nodes", "post", f"{_NODE}/cordon", {}, None),
    ("nodes", "post", f"{_NODE}/uncordon", {}, None),
    ("nodes", "post", f"{_CLUSTER}/nodes:cordon", {}, _BATCH),
    ("nodes", "post", f"{_CLUSTER}/nodes:uncordon", {}, _BATCH),
    ("nodes", "post", f"{_NODE}/drain", {}, {"options": {"force": True, "delete_emptydir_data": True}}),
    ("nodes", "patch", f"{_NODE}/labels", {}, {"set": {"dry-run-test": "x"}}),
    ("nodes", "patch", f"{_NODE}/annotations", {}, {"set": {"dry-run-test": "x"}}),
    ("nodes", "patch", f"{_NODE}/taints", {}, {"set": [{"key": "k", "effect": "NoSchedule"}]}),
    ("pods", "get", f"{_CLUSTER}/pods", {"namespace": "*"}, None),
    ("configmaps", "get", f"{_CLUSTER}/configmaps", {"namespace": "*"}, None),
    ("configmaps", "get", f"{_CLUSTER}/namespaces/default/configmaps/dry-run-app-config", {}, None),
]

_ROUTERS = Path(__file__).resolve().parents[2] / "app" / "api" / "v1"


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    get_settings.cache_clear()
    reset_dry_run_clusters()  # cordon / drain / patch mutate the fake cluster
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c
    reset_dry_run_clusters()
    get_settings.cache_clear()


@pytest.fixture
def headers(client) -> dict[str, str]:
    resp = client.post("/token", data={"username": "test_admin", "password": "secret"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _on_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class _RecordingKube:
    """Forwards to the dry-run client, noting where each Kubernetes call ran."""

    def __init__(self, inner, seen: dict[str, bool]) -> None:
        self._inner = inner
        self._seen = seen

    def __getattr__(self, name):
        target = getattr(self._inner, name)

        def call(*args, **kwargs):
            # Sticky: one call on the loop fails the route even if a later
            # call ran in a thread.
            self._seen["kube call"] = self._seen.get("kube call", False) or _on_event_loop()
            return target(*args, **kwargs)

        return call


@pytest.mark.parametrize(
    "method, path, params, body",
    [r[1:] for r in _ROUTES],
    ids=[f"{m.upper()} {p.removeprefix(_CLUSTER)}" for _, m, p, _, _ in _ROUTES],
)
def test_blocking_work_runs_off_the_event_loop(
    client, headers, monkeypatch, method, path, params, body
):
    seen: dict[str, bool] = {}

    class RecordingRepo(DryRunClusterRepository):
        def get_kube_client_config(self, cluster):
            seen["credentials"] = _on_event_loop()
            return super().get_kube_client_config(cluster)

    class RecordingFactory(kube_client.KubeClientFactory):
        def get_core_v1(self, cfg):
            seen["client"] = _on_event_loop()
            return _RecordingKube(super().get_core_v1(cfg), seen)

    client.app.dependency_overrides[get_cluster_repo] = RecordingRepo
    monkeypatch.setattr(kube_client, "KubeClientFactory", RecordingFactory)
    try:
        resp = client.request(method.upper(), path, headers=headers, params=params, json=body)
    finally:
        client.app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert seen == {"credentials": False, "client": False, "kube call": False}


# Routes that take the cluster repository but contact no cluster, with the reason.
_NO_CLUSTER_CONTACT = {
    ("GET", "/api/v1/clusters"): "lists the configured clusters from local files",
}


def test_no_router_keeps_a_private_copy_of_the_seam():
    """The dry-run switch for cluster credentials lives in get_cluster_repo
    only. A router building any repository itself — Yaml, Json, DryRun, or one
    added later — would bypass it, and in dry-run go looking for a real
    kubeconfig."""
    constructs = re.compile(r"\b\w*ClusterRepository\(")
    offenders = sorted(
        path.name for path in _ROUTERS.glob("*.py") if constructs.search(path.read_text())
    )
    assert offenders == []


def _depends_on(dependant, target) -> bool:
    return any(
        d.call is target or _depends_on(d, target) for d in dependant.dependencies
    )


def test_every_route_reaching_a_cluster_is_tabled(client):
    """Per route, not per file: every route whose dependency tree includes
    get_cluster_repo is either exercised by the table above or excused with a
    reason. A new route in an already-tabled router cannot slip past."""
    tabled = {(m.upper(), p) for _, m, p, _, _ in _ROUTES}
    missing = []
    for route in client.app.routes:
        if not isinstance(route, APIRoute) or not _depends_on(route.dependant, get_cluster_repo):
            continue
        for method in route.methods:
            if (method, route.path) in _NO_CLUSTER_CONTACT:
                continue
            if not any(
                m == method and route.path_regex.match(p) for m, p in tabled
            ):
                missing.append(f"{method} {route.path}")
    assert missing == [], "add these routes to _ROUTES"


def test_a_missing_cluster_raised_in_the_thread_is_still_a_404(monkeypatch, tmp_path):
    """call_kube resolves the cluster inside the worker thread; the exception
    must still reach the app's handler and become the structured 404."""
    monkeypatch.setenv("DRY_RUN_MODE", "false")
    monkeypatch.setenv("KUBECONFIG_BASE_PATH", str(tmp_path))  # no clusters configured
    get_settings.cache_clear()
    from app.main import create_app

    try:
        with TestClient(create_app()) as live:
            token = live.post(
                "/token", data={"username": "test_admin", "password": "secret"}
            ).json()["access_token"]
            resp = live.get(
                "/api/v1/clusters/no-such-cluster/nodes/n1",
                headers={"Authorization": f"Bearer {token}"},
            )
    finally:
        get_settings.cache_clear()

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CLUSTER_NOT_FOUND"
