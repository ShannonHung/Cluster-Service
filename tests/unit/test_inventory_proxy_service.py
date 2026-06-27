from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import DeployServiceError, ErrorCode, NotFoundException
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    NodeBastionResolution,
    NodeInfo,
    ClusterRef,
)
from app.services.inventory_service import InventoryProxyService


def _node_info() -> ClusterNodeInfo:
    return ClusterNodeInfo(
        node_type="baremetal",
        node=NodeInfo(id="1", name="node1"),
        cluster=ClusterRef(id="1", name="type1-cluster-c1", context="c1"),
    )


def _mapping() -> BastionMapping:
    return BastionMapping(patterns=["type1-.*"], runner="r", bastion="b", bastion_ip="10.0.0.5")


async def test_get_node_forwards():
    client = AsyncMock()
    client.get_node = AsyncMock(return_value=_node_info())
    svc = InventoryProxyService(client)
    result = await svc.get_node("node1")
    assert result.cluster.context == "c1"
    client.get_node.assert_awaited_once_with("node1")


async def test_list_mappings_forwards():
    client = AsyncMock()
    client.list_mappings = AsyncMock(return_value=[_mapping()])
    svc = InventoryProxyService(client)
    result = await svc.list_mappings("type1")
    assert result[0].bastion_ip == "10.0.0.5"
    client.list_mappings.assert_awaited_once_with("type1")


async def test_resolve_node_bastion_forwards_override():
    client = AsyncMock()
    client.resolve_node_bastion = AsyncMock(
        return_value=NodeBastionResolution(
            node_type="baremetal",
            node=NodeInfo(id="1", name="node1"),
            cluster=ClusterRef(id="1", name="type1-cluster-c1", context="c1"),
            bastion_type="type1",
            bastion_type_source="query_param",
            matched_mapping=_mapping(),
            matched_pattern="type1-.*",
        )
    )
    svc = InventoryProxyService(client)
    result = await svc.resolve_node_bastion("node1", bastion_type="type1")
    assert result.bastion_type_source == "query_param"
    client.resolve_node_bastion.assert_awaited_once_with("node1", bastion_type="type1")


async def test_resolve_cluster_bastion_forwards():
    client = AsyncMock()
    client.resolve_cluster_bastion = AsyncMock(
        return_value=ClusterBastionResolution(
            cluster_name="type1-cluster-c1",
            has_slash=False,
            bastion_type="type1",
            matched_mapping=_mapping(),
            matched_pattern="type1-.*",
        )
    )
    svc = InventoryProxyService(client)
    result = await svc.resolve_cluster_bastion("type1-cluster-c1")
    assert result.has_slash is False
    client.resolve_cluster_bastion.assert_awaited_once_with("type1-cluster-c1")


async def test_upstream_404_becomes_not_found():
    client = AsyncMock()
    client.get_node = AsyncMock(
        side_effect=DeployServiceError(http_status=404, body={"error": {"code": "NOT_FOUND", "message": "no node"}})
    )
    svc = InventoryProxyService(client)
    with pytest.raises(NotFoundException) as exc:
        await svc.get_node("ghost")
    assert exc.value.error_code == ErrorCode.INVENTORY_NOT_FOUND
    assert exc.value.http_status == 404


async def test_upstream_502_propagates():
    client = AsyncMock()
    client.list_mappings = AsyncMock(
        side_effect=DeployServiceError(http_status=503, body={"error": {"code": "X", "message": "down"}})
    )
    svc = InventoryProxyService(client)
    with pytest.raises(DeployServiceError):
        await svc.list_mappings("type1")
