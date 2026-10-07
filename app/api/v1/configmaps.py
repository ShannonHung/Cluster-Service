"""
app/api/v1/configmaps.py

ConfigMap endpoints (v1).

Routes:
  GET /api/v1/clusters/{cluster}/configmaps                              → list ConfigMaps (shape only, no values)
  GET /api/v1/clusters/{cluster}/namespaces/{namespace}/configmaps/{name} → read one ConfigMap's content

Listing requires ``cluster_api`` and never returns a value or an annotation.
Reading content is a separate, higher privilege (CONTEXT.md): it requires
``cluster_api`` *and* ``configmap_read`` — a step above cluster access, not a
separate way in.
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Path, Query, Request

from app.core.dependencies import get_cluster_repo, get_current_user
from app.domain.kubernetes_models import ConfigMapDetailData, ConfigMapListData
from app.domain.models import ApiResponse, User
from app.repositories.cluster_repository import ClusterRepository
from app.services.configmap_service import ConfigMapService
from app.services.kube_client import call_kube

router = APIRouter(prefix="/clusters", tags=["configmaps"])

# A Kubernetes namespace name (RFC 1123 label). Rejects "*": on a content read
# the namespace is part of the ConfigMap's identity, not a filter.
_NAMESPACE_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _split_csv(value: Optional[str]) -> list[str]:
    """Split a comma-separated query value into a trimmed, blank-free list."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@router.get(
    "/{cluster}/configmaps",
    response_model=ApiResponse[ConfigMapListData],
    summary="List ConfigMaps in a namespace",
    description=(
        "Lists ConfigMaps in the required ``namespace`` (use ``*`` for all "
        "namespaces), optionally filtered by ``name`` (comma-separated name "
        "prefixes, OR'd). Each entry shows the ConfigMap's shape — key names, "
        "labels, creation time — and never its values or annotations."
    ),
)
async def list_configmaps(
    request: Request,
    cluster: str,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    namespace: str = Query(..., min_length=1, description="Namespace to list from (required; '*' = all)."),
    name: Optional[str] = Query(None, description="Comma-separated name prefixes."),
    repo: ClusterRepository = Depends(get_cluster_repo),
) -> ApiResponse[ConfigMapListData]:
    data = await call_kube(
        repo,
        cluster,
        lambda kube: ConfigMapService().list_configmaps(
            cluster=cluster,
            namespace=namespace,
            kube=kube,
            name_prefixes=_split_csv(name),
        ),
    )
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/{cluster}/namespaces/{namespace}/configmaps/{name}",
    response_model=ApiResponse[ConfigMapDetailData],
    summary="Read one ConfigMap's content",
    description=(
        "Returns the ConfigMap's ``data`` and ``binary_data`` (base64, as sent "
        "by Kubernetes), with its labels and annotations. The "
        "``kubectl.kubernetes.io/last-applied-configuration`` annotation is "
        "left out: it is a stale copy of the values. Requires both "
        "``cluster_api`` and ``configmap_read``. 404 ``CONFIGMAP_NOT_FOUND`` "
        "when the ConfigMap or its namespace does not exist."
    ),
)
async def get_configmap(
    request: Request,
    cluster: str,
    name: str,
    current_user: Annotated[
        User, Depends(get_current_user(["cluster_api", "configmap_read"]))
    ],
    namespace: str = Path(..., pattern=_NAMESPACE_PATTERN, description="Namespace ('*' is not accepted)."),
    repo: ClusterRepository = Depends(get_cluster_repo),
) -> ApiResponse[ConfigMapDetailData]:
    data = await call_kube(
        repo,
        cluster,
        lambda kube: ConfigMapService().get_configmap(
            cluster=cluster, namespace=namespace, name=name, kube=kube
        ),
    )
    return ApiResponse(data=data, request_id=_request_id(request))
