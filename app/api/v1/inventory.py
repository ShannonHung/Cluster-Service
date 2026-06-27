"""
app/api/v1/inventory.py

Inventory proxy endpoints — forwarded to deploy-service's /api/v1/inventory/*.
All endpoints require the ``inventory_api`` scope.

  GET /api/v1/inventory/nodes/{node_name}
  GET /api/v1/inventory/mappings?type=
  GET /api/v1/inventory/nodes/{node_name}/bastion-resolution?bastion_type=
  GET /api/v1/inventory/cluster/bastion-resolution?cluster_name=
"""

from __future__ import annotations

import logging
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Query, Request

from app.api.v1.deploy import get_deploy_token_manager
from app.clients.deploy_service_client import DeployServiceClient
from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.core.token_manager import DeployServiceTokenManager
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    NodeBastionResolution,
)
from app.domain.models import ApiResponse, User
from app.services.inventory_service import InventoryProxyService

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inventory", tags=["inventory"])


def _get_inventory_service(
    token_manager: DeployServiceTokenManager = Depends(get_deploy_token_manager),
) -> InventoryProxyService:
    """Build InventoryProxyService backed by a live DeployServiceClient."""
    settings = get_settings()
    client = DeployServiceClient(
        base_url=settings.DEPLOY_SERVICE_URL,
        token_manager=token_manager,
    )
    return InventoryProxyService(client)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


@router.get(
    "/nodes/{node_name}",
    response_model=ApiResponse[ClusterNodeInfo],
    summary="Look up cluster node info by node name",
)
async def get_node(
    request: Request,
    node_name: str,
    svc: InventoryProxyService = Depends(_get_inventory_service),
    current_user: Annotated[User, Depends(get_current_user(["inventory_api"]))] = None,
) -> ApiResponse[ClusterNodeInfo]:
    data = await svc.get_node(node_name)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/mappings",
    response_model=ApiResponse[List[BastionMapping]],
    summary="List bastion-cluster mappings by type",
)
async def get_mappings(
    request: Request,
    type: str = Query(..., description="Bastion type name"),
    svc: InventoryProxyService = Depends(_get_inventory_service),
    current_user: Annotated[User, Depends(get_current_user(["inventory_api"]))] = None,
) -> ApiResponse[List[BastionMapping]]:
    data = await svc.list_mappings(type)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/nodes/{node_name}/bastion-resolution",
    response_model=ApiResponse[NodeBastionResolution],
    summary="Resolve node name to bastion runner",
)
async def get_node_bastion_resolution(
    request: Request,
    node_name: str,
    bastion_type: Optional[str] = Query(
        default=None, description="Override bastion type (default: derived in deploy-service)"
    ),
    svc: InventoryProxyService = Depends(_get_inventory_service),
    current_user: Annotated[User, Depends(get_current_user(["inventory_api"]))] = None,
) -> ApiResponse[NodeBastionResolution]:
    data = await svc.resolve_node_bastion(node_name, bastion_type=bastion_type)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/cluster/bastion-resolution",
    response_model=ApiResponse[ClusterBastionResolution],
    summary="Resolve a cluster name to a bastion runner",
)
async def get_cluster_bastion_resolution(
    request: Request,
    cluster_name: str = Query(..., description="Cluster name to resolve"),
    svc: InventoryProxyService = Depends(_get_inventory_service),
    current_user: Annotated[User, Depends(get_current_user(["inventory_api"]))] = None,
) -> ApiResponse[ClusterBastionResolution]:
    data = await svc.resolve_cluster_bastion(cluster_name)
    return ApiResponse(data=data, request_id=_request_id(request))
