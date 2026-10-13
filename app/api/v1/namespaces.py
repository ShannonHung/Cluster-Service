"""
app/api/v1/namespaces.py

Namespace endpoints (v1).

Routes:
  GET  /api/v1/clusters/{cluster}/namespaces        → list namespaces, filtered
  GET  /api/v1/clusters/{cluster}/namespaces/{name} → read one namespace
  POST /api/v1/clusters/{cluster}/namespaces        → create a namespace

All require the ``cluster_api`` scope. Vocabulary follows CONTEXT.md,
"Namespaces". There is deliberately no delete: it takes everything inside the
namespace with it, irreversibly.
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Path, Query, Request

from app.core.dependencies import get_cluster_repo, get_current_user
from app.domain.kubernetes_models import (
    NamespaceCreateRequest,
    NamespaceDetailData,
    NamespaceListData,
)
from app.domain.models import ApiResponse, User
from app.repositories.cluster_repository import ClusterRepository
from app.services.kube_client import call_kube
from app.services.namespace_service import NamespaceService

router = APIRouter(prefix="/clusters", tags=["namespaces"])

# A Kubernetes namespace name (RFC 1123 label).
_NAMESPACE_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _split_csv(value: Optional[str]) -> list[str]:
    """Split a comma-separated query value into a trimmed, blank-free list."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@router.get(
    "/{cluster}/namespaces",
    response_model=ApiResponse[NamespaceListData],
    summary="List namespaces",
    description=(
        "Lists every namespace, optionally filtered by ``name`` (comma-separated "
        "name prefixes), ``status`` (comma-separated, ``Active`` / "
        "``Terminating``, case-insensitive) and ``label_selector`` (evaluated by "
        "Kubernetes). Values within a filter are OR'd, filters are AND'd. Paging "
        "is internal: the response holds every match. An unknown status or a "
        "malformed label selector is 422."
    ),
)
async def list_namespaces(
    request: Request,
    cluster: str,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    name: Optional[str] = Query(None, description="Comma-separated name prefixes."),
    status: Optional[str] = Query(None, description="Comma-separated phases: Active, Terminating."),
    label_selector: Optional[str] = Query(None, description="Kubernetes label selector, e.g. team=a."),
    repo: ClusterRepository = Depends(get_cluster_repo),
) -> ApiResponse[NamespaceListData]:
    data = await call_kube(
        repo,
        cluster,
        lambda kube: NamespaceService().list_namespaces(
            cluster=cluster,
            kube=kube,
            name_prefixes=_split_csv(name),
            statuses=_split_csv(status),
            label_selector=label_selector or None,
        ),
    )
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/{cluster}/namespaces/{name}",
    response_model=ApiResponse[NamespaceDetailData],
    summary="Read one namespace",
    description=(
        "Returns the namespace's status (``Active`` / ``Terminating``), labels, "
        "annotations and timestamps. ``deletion_timestamp`` is set while the "
        "namespace is Terminating. The "
        "``kubectl.kubernetes.io/last-applied-configuration`` annotation is "
        "left out: it is a stale copy. 404 ``NAMESPACE_NOT_FOUND`` when it "
        "does not exist."
    ),
)
async def get_namespace(
    request: Request,
    cluster: str,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    name: str = Path(..., max_length=63, pattern=_NAMESPACE_PATTERN),
    repo: ClusterRepository = Depends(get_cluster_repo),
) -> ApiResponse[NamespaceDetailData]:
    data = await call_kube(
        repo,
        cluster,
        lambda kube: NamespaceService().get_namespace(cluster=cluster, name=name, kube=kube),
    )
    return ApiResponse(data=data, request_id=_request_id(request))


@router.post(
    "/{cluster}/namespaces",
    response_model=ApiResponse[NamespaceDetailData],
    status_code=201,
    summary="Create a namespace",
    description=(
        "Creates a namespace from a body holding only ``name`` (DNS-1123 label; "
        "not ``default``, not ``kube-*``) — any other field, ``labels`` "
        "included, is 422. The service adds no label or annotation of its own. "
        "409 ``NAMESPACE_ALREADY_EXISTS`` when the name is taken; "
        "``detail.status`` is ``Terminating`` when the existing namespace is "
        "still being deleted. A new namespace grants the service nothing inside "
        "it: its ConfigMaps stay unreadable until that namespace is granted."
    ),
)
async def create_namespace(
    request: Request,
    cluster: str,
    body: NamespaceCreateRequest,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    repo: ClusterRepository = Depends(get_cluster_repo),
) -> ApiResponse[NamespaceDetailData]:
    data = await call_kube(
        repo,
        cluster,
        lambda kube: NamespaceService().create_namespace(
            cluster=cluster, name=body.name, user=current_user.account, kube=kube
        ),
    )
    return ApiResponse(data=data, request_id=_request_id(request))
