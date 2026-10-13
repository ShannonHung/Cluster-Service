"""
app/api/v1/namespaces.py

Namespace endpoints (v1).

Routes:
  GET /api/v1/clusters/{cluster}/namespaces/{name} → read one namespace

Requires the ``cluster_api`` scope. Vocabulary follows CONTEXT.md, "Namespaces".
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from app.core.dependencies import get_cluster_repo, get_current_user
from app.domain.kubernetes_models import NamespaceDetailData
from app.domain.models import ApiResponse, User
from app.repositories.cluster_repository import ClusterRepository
from app.services.kube_client import call_kube
from app.services.namespace_service import NamespaceService

router = APIRouter(prefix="/clusters", tags=["namespaces"])

# A Kubernetes namespace name (RFC 1123 label).
_NAMESPACE_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


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
