"""
app/api/v1/pods.py

Pod query endpoint (v1).

Route:
  GET /api/v1/clusters/{cluster}/pods → list pods in a namespace, filtered.

Requires the ``cluster_api`` scope.
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Request

from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.domain.kubernetes_models import PodListData
from app.domain.models import ApiResponse, User
from app.repositories.cluster_repository import ClusterRepository
from app.repositories.yaml_cluster_repository import YamlClusterRepository
from app.services.kube_client import KubeClientFactory
from app.services.node_service import NodeService

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clusters", tags=["pods"])


def _get_cluster_repo() -> ClusterRepository:
    settings = get_settings()
    return YamlClusterRepository(settings.KUBECONFIG_BASE_PATH)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _split_csv(value: Optional[str]) -> list[str]:
    """Split a comma-separated query value into a trimmed, blank-free list."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@router.get(
    "/{cluster}/pods",
    response_model=ApiResponse[PodListData],
    summary="List pods in a namespace",
    description=(
        "Lists pods in the required ``namespace`` (use ``*`` for all "
        "namespaces), optionally filtered by ``node``, ``status`` (pod phase, "
        "case-insensitive), and ``pod_name`` (prefix match). Each filter "
        "accepts a comma-separated list; values within a filter are OR'd, "
        "filters are AND'd together."
    ),
)
async def list_pods(
    request: Request,
    cluster: str,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    namespace: str = Query(..., description="Namespace to list pods from (required; '*' = all)."),
    node: Optional[str] = Query(None, description="Comma-separated node names."),
    status: Optional[str] = Query(None, description="Comma-separated pod phases."),
    pod_name: Optional[str] = Query(None, description="Comma-separated name prefixes."),
    repo: ClusterRepository = Depends(_get_cluster_repo),
) -> ApiResponse[PodListData]:
    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    data = NodeService().list_pods(
        cluster=cluster,
        namespace=namespace,
        kube=kube,
        nodes=_split_csv(node),
        statuses=_split_csv(status),
        name_prefixes=_split_csv(pod_name),
    )
    return ApiResponse(data=data, request_id=_request_id(request))
