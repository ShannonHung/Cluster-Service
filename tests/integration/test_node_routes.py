"""Integration tests for the batch node action routes.

NodeService and ClusterRepository are overridden via dependency_overrides, and
KubeClientFactory is patched, so no Kubernetes cluster is needed. These cover
what service-level unit tests structurally cannot: the Pydantic request
constraints and the serialised response envelope.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.api.v1.nodes import _get_node_service
from app.core.dependencies import get_cluster_repo
from app.core.exceptions import NodeNotReadyException
from app.domain.kubernetes_models import (
    BatchNodeActionData,
    BatchNodeResult,
    BatchSummary,
)
from app.main import app


def _login(client, username="test_admin", password="secret") -> str:
    r = client.post("/token", data={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(client, username="test_admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {_login(client, username=username)}"}


@pytest.fixture
def fake_service():
    """Override NodeService + ClusterRepository, and stub the kube client."""
    svc = MagicMock()
    svc.cordon_many.return_value = BatchNodeActionData(
        cluster="c1",
        action="cordon",
        summary=BatchSummary(total=2, succeeded=1, failed=1),
        results=[
            BatchNodeResult(node="n1", status="success"),
            BatchNodeResult(
                node="n2",
                status="failed",
                error_code="NODE_NOT_FOUND",
                message="Node 'n2' not found in cluster 'c1'.",
                kube_status=404,
            ),
        ],
    )
    svc.uncordon_many.return_value = BatchNodeActionData(
        cluster="c1",
        action="uncordon",
        summary=BatchSummary(total=1, succeeded=1, failed=0),
        results=[BatchNodeResult(node="n1", status="success")],
    )

    app.dependency_overrides[_get_node_service] = lambda: svc
    app.dependency_overrides[get_cluster_repo] = lambda: MagicMock()
    with patch("app.services.kube_client.KubeClientFactory"):
        yield svc
    app.dependency_overrides.pop(_get_node_service, None)
    app.dependency_overrides.pop(get_cluster_repo, None)


# ── Auth ──────────────────────────────────────────────────────────────────────

def test_batch_cordon_requires_auth(client):
    r = client.post("/api/v1/clusters/c1/nodes:cordon", json={"nodes": ["n1"]})
    assert r.status_code == 401


# ── Request validation (the reason this seam exists) ──────────────────────────

def test_batch_cordon_rejects_empty_node_list(client, fake_service):
    r = client.post(
        "/api/v1/clusters/c1/nodes:cordon",
        json={"nodes": []},
        headers=_auth(client),
    )
    assert r.status_code == 422


def test_batch_cordon_rejects_oversized_batch(client, fake_service):
    r = client.post(
        "/api/v1/clusters/c1/nodes:cordon",
        json={"nodes": [f"n{i}" for i in range(101)]},
        headers=_auth(client),
    )
    assert r.status_code == 422


def test_batch_cordon_accepts_batch_at_the_limit(client, fake_service):
    r = client.post(
        "/api/v1/clusters/c1/nodes:cordon",
        json={"nodes": [f"n{i}" for i in range(100)]},
        headers=_auth(client),
    )
    assert r.status_code == 200, r.text


# ── Response shape ────────────────────────────────────────────────────────────

def test_batch_cordon_partial_failure_is_still_200(client, fake_service):
    r = client.post(
        "/api/v1/clusters/c1/nodes:cordon",
        json={"nodes": ["n1", "n2"], "reason": "monthly maintenance"},
        headers=_auth(client),
    )
    assert r.status_code == 200, r.text

    body = r.json()
    assert "request_id" in body
    data = body["data"]
    assert data["cluster"] == "c1"
    assert data["action"] == "cordon"
    assert data["summary"] == {"total": 2, "succeeded": 1, "failed": 1}
    assert data["results"][0] == {
        "node": "n1",
        "status": "success",
        "error_code": None,
        "message": None,
        "kube_status": None,
    }
    assert data["results"][1]["error_code"] == "NODE_NOT_FOUND"
    assert data["results"][1]["kube_status"] == 404


def test_batch_cordon_forwards_nodes_to_service(client, fake_service):
    client.post(
        "/api/v1/clusters/c1/nodes:cordon",
        json={"nodes": ["n1", "n2"]},
        headers=_auth(client),
    )
    assert fake_service.cordon_many.call_args.kwargs["node_names"] == ["n1", "n2"]
    assert fake_service.cordon_many.call_args.kwargs["cluster"] == "c1"


def test_batch_uncordon_ok(client, fake_service):
    r = client.post(
        "/api/v1/clusters/c1/nodes:uncordon",
        json={"nodes": ["n1"]},
        headers=_auth(client),
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["action"] == "uncordon"
    fake_service.uncordon_many.assert_called_once()


def test_batch_cordon_strips_whitespace_from_node_names(client, fake_service):
    """A stray space is a typo, not a different node."""
    client.post(
        "/api/v1/clusters/c1/nodes:cordon",
        json={"nodes": [" n1", "n2 "]},
        headers=_auth(client),
    )
    assert fake_service.cordon_many.call_args.kwargs["node_names"] == ["n1", "n2"]


# ── uncordon readiness gate ──────────────────────────────────────────────────

def test_uncordon_not_ready_returns_409_envelope(client, fake_service):
    """The gate must surface as 409 with the standard error envelope.

    409 rather than 400 because the request is well-formed and no parameter
    would change the outcome — what has to change is the node.
    """
    fake_service.uncordon.side_effect = NodeNotReadyException(
        node_name="n1", status="NotReady",
    )

    r = client.post(
        "/api/v1/clusters/c1/nodes/n1/uncordon", headers=_auth(client),
    )

    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "NODE_NOT_READY"
    assert body["error"]["detail"] == {"node": "n1", "status": "NotReady"}
    assert "request_id" in body


def test_batch_uncordon_reports_not_ready_per_node(client, fake_service):
    """A NotReady node is a per-node failure: the batch still returns 200 and
    the nodes that succeeded are still reported."""
    fake_service.uncordon_many.return_value = BatchNodeActionData(
        cluster="c1",
        action="uncordon",
        summary=BatchSummary(total=2, succeeded=1, failed=1),
        results=[
            BatchNodeResult(node="n1", status="success"),
            BatchNodeResult(
                node="n2",
                status="failed",
                error_code="NODE_NOT_READY",
                message="Node 'n2' is NotReady, so it cannot be uncordoned.",
                kube_status=409,
            ),
        ],
    )

    r = client.post(
        "/api/v1/clusters/c1/nodes:uncordon",
        json={"nodes": ["n1", "n2"]},
        headers=_auth(client),
    )

    assert r.status_code == 200
    data = r.json()["data"]
    assert data["summary"] == {"total": 2, "succeeded": 1, "failed": 1}
    failed = [x for x in data["results"] if x["status"] == "failed"]
    assert failed[0]["error_code"] == "NODE_NOT_READY"
    assert failed[0]["kube_status"] == 409
