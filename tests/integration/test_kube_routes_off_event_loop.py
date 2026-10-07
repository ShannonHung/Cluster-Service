"""
tests/integration/test_kube_routes_off_event_loop.py

Kubernetes routes must not block the event loop (CLAUDE.md, "Never call a
Kubernetes service inline from a route").

Three things block, and all three are checked per route:

- resolving credentials — reads a kubeconfig, and may run an exec credential
  plugin (EKS / GKE) that takes seconds
- building the client — can write a CA temp file
- the Kubernetes call itself — the SDK blocks on urllib3 sockets

Any one of them on the event loop stalls every other request, health checks
included, for as long as it takes. Each is recorded from inside the route's own
collaborators, so the test fails if any of the three moves back.

The node routes under ``nodes.py`` thread the service call but still resolve
credentials and build the client on the event loop; they join this table when
the cluster-repository seam is shared (#36).
"""

from __future__ import annotations

import asyncio
import importlib

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.repositories.dry_run_cluster_repository import DryRunClusterRepository
from app.services.kube_client import KubeClientFactory

_CLUSTER = "/api/v1/clusters/any-cluster"

# (router module, path, query params)
_ROUTES = [
    ("clusters", f"{_CLUSTER}/nodes", {}),
    ("pods", f"{_CLUSTER}/pods", {"namespace": "*"}),
    ("configmaps", f"{_CLUSTER}/configmaps", {"namespace": "*"}),
    ("configmaps", f"{_CLUSTER}/namespaces/default/configmaps/dry-run-app-config", {}),
]


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
            self._seen["kube call"] = _on_event_loop()
            return target(*args, **kwargs)

        return call


@pytest.mark.parametrize(
    "module_name, path, params",
    _ROUTES,
    ids=[f"{m}:{p.removeprefix(_CLUSTER)}" for m, p, _ in _ROUTES],
)
def test_blocking_work_runs_off_the_event_loop(
    client, headers, monkeypatch, module_name, path, params
):
    module = importlib.import_module(f"app.api.v1.{module_name}")
    seen: dict[str, bool] = {}

    class RecordingRepo(DryRunClusterRepository):
        def get_kube_client_config(self, cluster):
            seen["credentials"] = _on_event_loop()
            return super().get_kube_client_config(cluster)

    class RecordingFactory(KubeClientFactory):
        def get_core_v1(self, cfg):
            seen["client"] = _on_event_loop()
            return _RecordingKube(super().get_core_v1(cfg), seen)

    client.app.dependency_overrides[module._get_cluster_repo] = RecordingRepo
    monkeypatch.setattr(module, "KubeClientFactory", RecordingFactory)
    try:
        resp = client.get(path, headers=headers, params=params)
    finally:
        client.app.dependency_overrides.clear()

    assert resp.status_code == 200, resp.text
    assert seen == {"credentials": False, "client": False, "kube call": False}
