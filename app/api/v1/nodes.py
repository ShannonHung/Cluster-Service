"""
app/api/v1/nodes.py

Node-level operation endpoints (v1).

Routes:
  GET   /api/v1/clusters/{cluster}/nodes/{node}             → get node detail (no pods)
  POST  /api/v1/clusters/{cluster}/nodes/{node}/cordon      → cordon a node
  POST  /api/v1/clusters/{cluster}/nodes/{node}/uncordon    → uncordon a node
  POST  /api/v1/clusters/{cluster}/nodes/{node}/drain       → drain a node
  PATCH /api/v1/clusters/{cluster}/nodes/{node}/labels      → set/remove labels
  PATCH /api/v1/clusters/{cluster}/nodes/{node}/annotations → set/remove annotations
  PATCH /api/v1/clusters/{cluster}/nodes/{node}/taints      → set/remove taints

All endpoints require the ``cluster_api`` scope.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.domain.kubernetes_models import (
    DrainActionData,
    DrainRequest,
    NodeActionData,
    NodeDetailData,
    NodeMetadataData,
    NodePatchRequest,
    NodeTaintData,
    NodeTaintRequest,
)
from app.domain.models import ApiResponse, User
from app.repositories.cluster_repository import ClusterRepository
from app.repositories.yaml_cluster_repository import YamlClusterRepository
from app.services.kube_client import KubeClientFactory
from app.services.node_service import NodeService

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clusters", tags=["nodes"])


# ── Dependency providers ──────────────────────────────────────────────────────

def _get_cluster_repo() -> ClusterRepository:
    settings = get_settings()
    return YamlClusterRepository(settings.KUBECONFIG_BASE_PATH)


def _get_node_service() -> NodeService:
    settings = get_settings()
    return NodeService(
        cordon_label_reason=settings.CORDON_LABEL_REASON,
        cordon_label_by=settings.CORDON_LABEL_BY,
    )


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")



# ── GET …/{cluster}/nodes/{node} ─────────────────────────────────────────────

@router.get(
    "/{cluster}/nodes/{node}",
    response_model=ApiResponse[NodeDetailData],
    summary="Get node detail",
    description=(
        "Returns full node information — status, roles, kubelet version, labels, "
        "annotations, schedulability. Pods are queried via "
        "GET /clusters/{cluster}/pods."
    ),
)
async def get_node(
    request: Request,
    cluster: str,
    node: str,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    repo: ClusterRepository = Depends(_get_cluster_repo),
    svc: NodeService = Depends(_get_node_service),
) -> ApiResponse[NodeDetailData]:
    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    data = svc.get_node(cluster=cluster, node_name=node, kube=kube)
    return ApiResponse(data=data, request_id=_request_id(request))

# ── POST …/cordon ─────────────────────────────────────────────────────────────

@router.post(
    "/{cluster}/nodes/{node}/cordon",
    response_model=ApiResponse[NodeActionData],
    summary="Cordon a node",
    description=(
        "Marks the node as unschedulable so no new pods are placed on it. "
        "Stamps ``cordon_reason`` and ``cordon_by`` labels configured in Settings."
    ),
)
async def cordon_node(
    request: Request,
    cluster: str,
    node: str,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    repo: ClusterRepository = Depends(_get_cluster_repo),
    svc: NodeService = Depends(_get_node_service),
) -> ApiResponse[NodeActionData]:
    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    data = svc.cordon(cluster=cluster, node_name=node, kube=kube)
    return ApiResponse(data=data, request_id=_request_id(request))

# ── POST …/uncordon ───────────────────────────────────────────────────────────

@router.post(
    "/{cluster}/nodes/{node}/uncordon",
    response_model=ApiResponse[NodeActionData],
    summary="Uncordon a node",
    description="Re-enables scheduling and removes the cordon labels.",
)
async def uncordon_node(
    request: Request,
    cluster: str,
    node: str,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    repo: ClusterRepository = Depends(_get_cluster_repo),
    svc: NodeService = Depends(_get_node_service),
) -> ApiResponse[NodeActionData]:
    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    data = svc.uncordon(cluster=cluster, node_name=node, kube=kube)
    return ApiResponse(data=data, request_id=_request_id(request))


# ── POST …/drain ──────────────────────────────────────────────────────────────

@router.post(
    "/{cluster}/nodes/{node}/drain",
    response_model=ApiResponse[DrainActionData],
    summary="Drain a node",
    description=(
        "Cordons the node, then evicts/deletes all eligible pods. "
        "DaemonSet, mirror, and completed pods are always skipped. "
        "Returns the list of pods that were drained.\n\n"
        "Set ``dry_run=true`` to validate without making any changes."
    ),
)
async def drain_node(
    request: Request,
    cluster: str,
    node: str,
    body: DrainRequest = DrainRequest(),
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))] = None,
    repo: ClusterRepository = Depends(_get_cluster_repo),
    svc: NodeService = Depends(_get_node_service),
) -> ApiResponse[DrainActionData]:
    _logger.info(
        "Drain requested | cluster=%s | node=%s | dry_run=%s | reason=%s",
        cluster, node, body.dry_run, body.reason,
    )

    # Short-circuit: dry-run never touches the cluster.
    if body.dry_run:
        return ApiResponse(
            data=DrainActionData(cluster=cluster, node=node, dry_run=True),
            request_id=_request_id(request),
        )

    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    data = svc.drain(cluster=cluster, node_name=node, kube=kube, options=body.options)
    return ApiResponse(data=data, request_id=_request_id(request))


# ── PATCH …/labels ────────────────────────────────────────────────────────────

@router.patch(
    "/{cluster}/nodes/{node}/labels",
    response_model=ApiResponse[NodeMetadataData],
    summary="Set or remove node labels",
    description=(
        "Set ``set`` to add/overwrite labels, ``remove`` to delete keys. "
        "Response contains the node's **current** labels and annotations after the patch."
    ),
)
async def patch_node_labels(
    request: Request,
    cluster: str,
    node: str,
    body: NodePatchRequest = NodePatchRequest(),
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))] = None,
    repo: ClusterRepository = Depends(_get_cluster_repo),
    svc: NodeService = Depends(_get_node_service),
) -> ApiResponse[NodeMetadataData]:
    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    data = svc.label_node(
        cluster=cluster,
        node_name=node,
        kube=kube,
        set_labels=body.set,
        remove_labels=body.remove,
    )
    return ApiResponse(data=data, request_id=_request_id(request))


# ── PATCH …/annotations ───────────────────────────────────────────────────────

@router.patch(
    "/{cluster}/nodes/{node}/annotations",
    response_model=ApiResponse[NodeMetadataData],
    summary="Set or remove node annotations",
    description=(
        "Set ``set`` to add/overwrite annotations, ``remove`` to delete keys. "
        "Response contains the node's **current** labels and annotations after the patch."
    ),
)
async def patch_node_annotations(
    request: Request,
    cluster: str,
    node: str,
    body: NodePatchRequest = NodePatchRequest(),
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))] = None,
    repo: ClusterRepository = Depends(_get_cluster_repo),
    svc: NodeService = Depends(_get_node_service),
) -> ApiResponse[NodeMetadataData]:
    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    data = svc.annotate_node(
        cluster=cluster,
        node_name=node,
        kube=kube,
        set_annotations=body.set,
        remove_annotations=body.remove,
    )
    return ApiResponse(data=data, request_id=_request_id(request))


# ── PATCH …/taints ────────────────────────────────────────────────────────────

@router.patch(
    "/{cluster}/nodes/{node}/taints",
    response_model=ApiResponse[NodeTaintData],
    summary="Set or remove node taints",
    description=(
        "Set ``set`` to add/overwrite taints (a taint with the same key+effect "
        "overwrites the value), ``remove`` to delete taints by key+effect. "
        "``effect`` must be NoSchedule, PreferNoSchedule, or NoExecute. "
        "Response contains the node's **current** taints after the patch."
    ),
)
async def patch_node_taints(
    request: Request,
    cluster: str,
    node: str,
    body: NodeTaintRequest = NodeTaintRequest(),
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))] = None,
    repo: ClusterRepository = Depends(_get_cluster_repo),
    svc: NodeService = Depends(_get_node_service),
) -> ApiResponse[NodeTaintData]:
    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    data = svc.taint_node(
        cluster=cluster,
        node_name=node,
        kube=kube,
        set_taints=body.set,
        remove_taints=body.remove,
    )
    return ApiResponse(data=data, request_id=_request_id(request))
