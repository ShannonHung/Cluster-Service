from unittest.mock import AsyncMock

import pytest

from app.clients.deploy_service_client import DeployServiceClient
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    NodeBastionResolution,
)


def _client() -> DeployServiceClient:
    tm = AsyncMock()
    tm.get_token = AsyncMock(return_value="t")
    return DeployServiceClient(base_url="http://deploy", token_manager=tm)


async def test_get_node_unwraps_data(monkeypatch):
    c = _client()
    payload = {
        "data": {
            "node_type": "baremetal",
            "node": {"id": "1", "name": "node1", "labels": {}},
            "cluster": {"id": "1", "name": "type1-cluster-c1", "context": "c1"},
        },
        "request_id": "r1",
    }
    captured = {}

    async def fake_req(method, path, context, **kwargs):
        captured.update(method=method, path=path, kwargs=kwargs)
        return payload

    monkeypatch.setattr(c, "_request_with_retry", fake_req)
    result = await c.get_node("node1")
    assert isinstance(result, ClusterNodeInfo)
    assert result.cluster.context == "c1"
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/inventory/nodes/node1"


async def test_list_mappings_unwraps_list(monkeypatch):
    c = _client()
    payload = {
        "data": [
            {"patterns": ["type1-.*"], "runner": "r", "bastion": "b", "bastion_ip": "10.0.0.5"}
        ],
        "request_id": "r1",
    }
    captured = {}

    async def fake_req(method, path, context, **kwargs):
        captured.update(path=path, kwargs=kwargs)
        return payload

    monkeypatch.setattr(c, "_request_with_retry", fake_req)
    result = await c.list_mappings("type1")
    assert len(result) == 1
    assert isinstance(result[0], BastionMapping)
    assert captured["path"] == "/api/v1/inventory/mappings"
    assert captured["kwargs"]["params"] == {"type": "type1"}


async def test_resolve_node_bastion_forwards_override(monkeypatch):
    c = _client()
    payload = {
        "data": {
            "node_type": "baremetal",
            "node": {"id": "1", "name": "node1", "labels": {}},
            "cluster": {"id": "1", "name": "type1-cluster-c1", "context": "c1"},
            "bastion_type": "type1",
            "bastion_type_source": "query_param",
            "matched_mapping": {
                "patterns": ["type1-.*"], "runner": "r", "bastion": "b", "bastion_ip": "10.0.0.5"
            },
            "matched_pattern": "type1-.*",
        },
        "request_id": "r1",
    }
    captured = {}

    async def fake_req(method, path, context, **kwargs):
        captured.update(path=path, kwargs=kwargs)
        return payload

    monkeypatch.setattr(c, "_request_with_retry", fake_req)
    result = await c.resolve_node_bastion("node1", bastion_type="type1")
    assert isinstance(result, NodeBastionResolution)
    assert captured["path"] == "/api/v1/inventory/nodes/node1/bastion-resolution"
    assert captured["kwargs"]["params"] == {"bastion_type": "type1"}


async def test_resolve_node_bastion_omits_none_override(monkeypatch):
    c = _client()
    payload = {
        "data": {
            "node_type": "baremetal",
            "node": {"id": "1", "name": "node1", "labels": {}},
            "cluster": {"id": "1", "name": "type1-cluster-c1", "context": "c1"},
            "bastion_type": "type1",
            "bastion_type_source": "config",
            "matched_mapping": {
                "patterns": ["type1-.*"], "runner": "r", "bastion": "b", "bastion_ip": "10.0.0.5"
            },
            "matched_pattern": "type1-.*",
        },
        "request_id": "r1",
    }
    captured = {}

    async def fake_req(method, path, context, **kwargs):
        captured.update(kwargs=kwargs)
        return payload

    monkeypatch.setattr(c, "_request_with_retry", fake_req)
    await c.resolve_node_bastion("node1")
    assert captured["kwargs"].get("params") == {}


async def test_resolve_cluster_bastion(monkeypatch):
    c = _client()
    payload = {
        "data": {
            "cluster_name": "type1-cluster-c1",
            "has_slash": False,
            "bastion_type": "type1",
            "matched_mapping": {
                "patterns": ["type1-.*"], "runner": "r", "bastion": "b", "bastion_ip": "10.0.0.5"
            },
            "matched_pattern": "type1-.*",
        },
        "request_id": "r1",
    }
    captured = {}

    async def fake_req(method, path, context, **kwargs):
        captured.update(path=path, kwargs=kwargs)
        return payload

    monkeypatch.setattr(c, "_request_with_retry", fake_req)
    result = await c.resolve_cluster_bastion("type1-cluster-c1")
    assert isinstance(result, ClusterBastionResolution)
    assert captured["path"] == "/api/v1/inventory/cluster/bastion-resolution"
    assert captured["kwargs"]["params"] == {"cluster_name": "type1-cluster-c1"}
