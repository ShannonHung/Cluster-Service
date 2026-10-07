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

import asyncio
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Path, Query, Request

from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.domain.kubernetes_models import ConfigMapDetailData, ConfigMapListData
from app.domain.models import ApiResponse, User
from app.repositories.cluster_repository import ClusterRepository
from app.repositories.dry_run_cluster_repository import DryRunClusterRepository
from app.repositories.yaml_cluster_repository import YamlClusterRepository
from app.services.configmap_service import ConfigMapService
from app.services.kube_client import KubeClientFactory

router = APIRouter(prefix="/clusters", tags=["configmaps"])

# A Kubernetes namespace name (RFC 1123 label). Rejects "*": on a content read
# the namespace is part of the ConfigMap's identity, not a filter.
_NAMESPACE_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"


def _get_cluster_repo() -> ClusterRepository:
    """Resolve the cluster-config source; dry-run swaps it before any
    kubeconfig is read (see app/api/v1/pods.py)."""
    settings = get_settings()
    if settings.DRY_RUN_MODE:
        return DryRunClusterRepository()
    return YamlClusterRepository(settings.KUBECONFIG_BASE_PATH)


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
    repo: ClusterRepository = Depends(_get_cluster_repo),
) -> ApiResponse[ConfigMapListData]:
    def _list() -> ConfigMapListData:
        # Credentials and client construction block too — a kubeconfig read, an
        # exec credential plugin (EKS / GKE), a CA temp file — so they run in
        # the worker thread with the call itself, not on the event loop.
        cfg = repo.get_kube_client_config(cluster)
        kube = KubeClientFactory().get_core_v1(cfg)
        return ConfigMapService().list_configmaps(
            cluster=cluster,
            namespace=namespace,
            kube=kube,
            name_prefixes=_split_csv(name),
        )

    data = await asyncio.to_thread(_list)
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
    repo: ClusterRepository = Depends(_get_cluster_repo),
) -> ApiResponse[ConfigMapDetailData]:
    def _read() -> ConfigMapDetailData:
        cfg = repo.get_kube_client_config(cluster)
        kube = KubeClientFactory().get_core_v1(cfg)
        return ConfigMapService().get_configmap(
            cluster=cluster, namespace=namespace, name=name, kube=kube
        )

    data = await asyncio.to_thread(_read)
    return ApiResponse(data=data, request_id=_request_id(request))
