"""Integration tests for the inventory proxy router. InventoryProxyService is
overridden with a fake via dependency_overrides so no real deploy-service is
needed."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from app.main import app
from app.api.v1.inventory import _get_inventory_service
from app.core.exceptions import ErrorCode, NotFoundException
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    ClusterRef,
    NodeBastionResolution,
    NodeInfo,
)


def _login(client, username="test_admin", password="secret") -> str:
    r = client.post("/token", data={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _mapping() -> BastionMapping:
    return BastionMapping(patterns=["type1-.*"], runner="r", bastion="b", bastion_ip="10.0.0.5")


@pytest.fixture
def fake_service():
    svc = AsyncMock()
    svc.get_node = AsyncMock(
        return_value=ClusterNodeInfo(
            node_type="baremetal",
            node=NodeInfo(id="1", name="node1"),
            cluster=ClusterRef(id="1", name="type1-cluster-c1", context="c1"),
        )
    )
    svc.list_mappings = AsyncMock(return_value=[_mapping()])
    svc.resolve_node_bastion = AsyncMock(
        return_value=NodeBastionResolution(
            node_type="baremetal",
            node=NodeInfo(id="1", name="node1"),
            cluster=ClusterRef(id="1", name="type1-cluster-c1", context="c1"),
            bastion_type="type1",
            bastion_type_source="config",
            matched_mapping=_mapping(),
            matched_pattern="type1-.*",
        )
    )
    svc.resolve_cluster_bastion = AsyncMock(
        return_value=ClusterBastionResolution(
            cluster_name="type1-cluster-c1",
            has_slash=False,
            bastion_type="type1",
            matched_mapping=_mapping(),
            matched_pattern="type1-.*",
        )
    )
    app.dependency_overrides[_get_inventory_service] = lambda: svc
    yield svc
    app.dependency_overrides.pop(_get_inventory_service, None)


def test_get_node_requires_auth(client):
    r = client.get("/api/v1/inventory/nodes/node1")
    assert r.status_code == 401


def test_get_node_requires_scope(client, fake_service):
    # test_operator has only cluster_api, not inventory_api
    token = _login(client, username="test_operator")
    r = client.get(
        "/api/v1/inventory/nodes/node1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


def test_get_node_ok(client, fake_service):
    token = _login(client)
    r = client.get(
        "/api/v1/inventory/nodes/node1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["cluster"]["context"] == "c1"
    assert "request_id" in body
    fake_service.get_node.assert_awaited_once_with("node1")


def test_list_mappings_forwards_type(client, fake_service):
    token = _login(client)
    r = client.get(
        "/api/v1/inventory/mappings",
        params={"type": "type1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"][0]["bastion_ip"] == "10.0.0.5"
    fake_service.list_mappings.assert_awaited_once_with("type1")


def test_node_bastion_resolution_forwards_override(client, fake_service):
    token = _login(client)
    r = client.get(
        "/api/v1/inventory/nodes/node1/bastion-resolution",
        params={"bastion_type": "type1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["bastion_type"] == "type1"
    fake_service.resolve_node_bastion.assert_awaited_once_with("node1", bastion_type="type1")


def test_cluster_bastion_resolution_ok(client, fake_service):
    token = _login(client)
    r = client.get(
        "/api/v1/inventory/cluster/bastion-resolution",
        params={"cluster_name": "type1-cluster-c1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["cluster_name"] == "type1-cluster-c1"
    fake_service.resolve_cluster_bastion.assert_awaited_once_with("type1-cluster-c1")


def test_get_node_404_surfaces(client, fake_service):
    fake_service.get_node = AsyncMock(side_effect=NotFoundException("no node"))
    token = _login(client)
    r = client.get(
        "/api/v1/inventory/nodes/ghost",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404, r.text
