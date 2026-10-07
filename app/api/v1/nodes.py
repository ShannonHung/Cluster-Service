"""
app/api/v1/nodes.py

Node-level operation endpoints (v1).

Routes:
  GET   /api/v1/clusters/{cluster}/nodes/{node}             → get node detail (no pods)
  POST  /api/v1/clusters/{cluster}/nodes/{node}/cordon      → cordon a node
  POST  /api/v1/clusters/{cluster}/nodes/{node}/uncordon    → uncordon a node
  POST  /api/v1/clusters/{cluster}/nodes/{node}/drain       → drain a node
  POST  /api/v1/clusters/{cluster}/nodes:cordon             → cordon several nodes (batch)
  POST  /api/v1/clusters/{cluster}/nodes:uncordon           → uncordon several nodes (batch)
  PATCH /api/v1/clusters/{cluster}/nodes/{node}/labels      → set/remove labels
  PATCH /api/v1/clusters/{cluster}/nodes/{node}/annotations → set/remove annotations
  PATCH /api/v1/clusters/{cluster}/nodes/{node}/taints      → set/remove taints

All endpoints require the ``cluster_api`` scope.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.domain.kubernetes_models import (
    BatchNodeActionData,
    BatchNodeRequest,
    DrainActionData,
    DrainRequest,
    NodeActionData,
    NodeAnnotationsData,
    NodeDetailData,
    NodeLabelsData,
    NodePatchRequest,
    NodeTaintData,
    NodeTaintRequest,
)
from app.domain.models import ApiResponse, User
from app.repositories.cluster_repository import ClusterRepository
from app.repositories.dry_run_cluster_repository import DryRunClusterRepository
from app.repositories.yaml_cluster_repository import YamlClusterRepository
from app.services.kube_client import KubeClientFactory
from app.services.node_service import NodeService

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clusters", tags=["nodes"])


# ── Dependency providers ──────────────────────────────────────────────────────

def _get_cluster_repo() -> ClusterRepository:
    """Resolve the cluster-config source.

    In dry-run this is swapped *before* any kubeconfig is read: the real
    repositories resolve a cluster by reading a file from
    KUBECONFIG_BASE_PATH, so stubbing only the client factory would still
    demand credentials on disk. A dry-run instance holds none. See
    app/repositories/dry_run_cluster_repository.py.
    """
    settings = get_settings()
    if settings.DRY_RUN_MODE:
        return DryRunClusterRepository()
    return YamlClusterRepository(settings.KUBECONFIG_BASE_PATH)


def _get_node_service() -> NodeService:
    return NodeService()


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
    data = await asyncio.to_thread(
        svc.get_node, cluster=cluster, node_name=node, kube=kube,
    )
    return ApiResponse(data=data, request_id=_request_id(request))

# ── POST …/cordon ─────────────────────────────────────────────────────────────

@router.post(
    "/{cluster}/nodes/{node}/cordon",
    response_model=ApiResponse[NodeActionData],
    summary="Cordon a node",
    description="Marks the node as unschedulable so no new pods are placed on it.",
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
    data = await asyncio.to_thread(
        svc.cordon, cluster=cluster, node_name=node, kube=kube,
    )
    return ApiResponse(data=data, request_id=_request_id(request))

# ── POST …/uncordon ───────────────────────────────────────────────────────────

@router.post(
    "/{cluster}/nodes/{node}/uncordon",
    response_model=ApiResponse[NodeActionData],
    summary="Uncordon a node",
    description=(
        "Re-enables scheduling on the node.\n\n"
        "The node must currently be **Ready**. Uncordoning declares a node fit "
        "to receive pods, so this is refused with 409 `NODE_NOT_READY` for a "
        "NotReady or Unknown node — there is no override, because no change to "
        "the request can make an unhealthy node healthy.\n\n"
        "Note this guards against acting on a stale view of the cluster, not "
        "against instability: an uncordoned node that flaps in and out of Ready "
        "will still be filled during its Ready windows."
    ),
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
    data = await asyncio.to_thread(
        svc.uncordon, cluster=cluster, node_name=node, kube=kube,
    )
    return ApiResponse(data=data, request_id=_request_id(request))


# ── POST …/nodes:cordon ───────────────────────────────────────────────────────

@router.post(
    "/{cluster}/nodes:cordon",
    response_model=ApiResponse[BatchNodeActionData],
    summary="Cordon several nodes",
    description=(
        "Marks each listed node as unschedulable. Always returns 200 when the "
        "cluster itself is reachable — per-node outcomes are reported in "
        "``results``, so a failed node never hides the nodes that succeeded.\n\n"
        "Duplicate names are de-duplicated. Cluster-level failures (unknown "
        "cluster, unreachable API server) are returned as a normal error "
        "response instead, since no node was attempted."
    ),
)
async def cordon_nodes(
    request: Request,
    cluster: str,
    body: BatchNodeRequest,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    repo: ClusterRepository = Depends(_get_cluster_repo),
    svc: NodeService = Depends(_get_node_service),
) -> ApiResponse[BatchNodeActionData]:
    return await _run_batch(
        request=request,
        cluster=cluster,
        body=body,
        user=current_user,
        repo=repo,
        svc=svc,
        action="cordon",
    )


# ── POST …/nodes:uncordon ─────────────────────────────────────────────────────

@router.post(
    "/{cluster}/nodes:uncordon",
    response_model=ApiResponse[BatchNodeActionData],
    summary="Uncordon several nodes",
    description=(
        "Re-enables scheduling on each listed node. Response semantics match "
        "the batch cordon endpoint.\n\n"
        "Each node must be Ready; one that is not fails on its own with "
        "`NODE_NOT_READY` and never aborts the batch — including when every "
        "node in the batch fails that way.\n\n"
        "Readiness for the whole batch comes from a single `list nodes` call, "
        "so the credentials for this cluster need **list** on nodes in addition "
        "to **patch**. A token holding only patch fails the batch outright "
        "rather than returning per-node results."
    ),
)
async def uncordon_nodes(
    request: Request,
    cluster: str,
    body: BatchNodeRequest,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    repo: ClusterRepository = Depends(_get_cluster_repo),
    svc: NodeService = Depends(_get_node_service),
) -> ApiResponse[BatchNodeActionData]:
    return await _run_batch(
        request=request,
        cluster=cluster,
        body=body,
        user=current_user,
        repo=repo,
        svc=svc,
        action="uncordon",
    )


async def _run_batch(
    *,
    request: Request,
    cluster: str,
    body: BatchNodeRequest,
    user: User,
    repo: ClusterRepository,
    svc: NodeService,
    action: str,
) -> ApiResponse[BatchNodeActionData]:
    """Shared body for the two batch routes.

    NodeService is synchronous (blocking kubernetes client), so the batch runs
    on a worker thread — otherwise a batch of N nodes would block the event
    loop for N round-trips and the process would serve no other request,
    health checks included, while it ran.
    """
    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    batch = svc.cordon_many if action == "cordon" else svc.uncordon_many

    data = await asyncio.to_thread(
        batch, cluster=cluster, node_names=body.nodes, kube=kube,
    )

    failed = [r.node for r in data.results if r.status == "failed"]
    _logger.info(
        "Batch %s | user=%s | cluster=%s | requested=%d | succeeded=%d | "
        "failed=%d | failed_nodes=%s | reason=%s",
        action,
        user.account,
        cluster,
        data.summary.total,
        data.summary.succeeded,
        data.summary.failed,
        failed,
        body.reason,
    )
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

    # The drain wait budget is server-owned (DRAIN_DEFAULT_TIMEOUT_SECONDS) and
    # resolved inside the service — the client cannot set a per-request timeout.
    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    data = await asyncio.to_thread(
        svc.drain, cluster=cluster, node_name=node, kube=kube, options=body.options,
    )
    return ApiResponse(data=data, request_id=_request_id(request))


# ── PATCH …/labels ────────────────────────────────────────────────────────────

@router.patch(
    "/{cluster}/nodes/{node}/labels",
    response_model=ApiResponse[NodeLabelsData],
    summary="Set or remove node labels",
    description=(
        "Set ``set`` to add/overwrite labels, ``remove`` to delete keys. "
        "Response contains the node's **current** labels after the patch."
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
) -> ApiResponse[NodeLabelsData]:
    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    data = await asyncio.to_thread(
        svc.label_node,
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
    response_model=ApiResponse[NodeAnnotationsData],
    summary="Set or remove node annotations",
    description=(
        "Set ``set`` to add/overwrite annotations, ``remove`` to delete keys. "
        "Response contains the node's **current** annotations after the patch."
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
) -> ApiResponse[NodeAnnotationsData]:
    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    data = await asyncio.to_thread(
        svc.annotate_node,
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
    data = await asyncio.to_thread(
        svc.taint_node,
        cluster=cluster,
        node_name=node,
        kube=kube,
        set_taints=body.set,
        remove_taints=body.remove,
    )
    return ApiResponse(data=data, request_id=_request_id(request))
