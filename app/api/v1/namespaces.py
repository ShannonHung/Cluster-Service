"""
app/api/v1/namespaces.py

Namespace endpoints (v1).

Routes:
  GET  /api/v1/clusters/{cluster}/namespaces        → list namespaces, filtered
  GET  /api/v1/clusters/{cluster}/namespaces/{name} → read one namespace
  POST /api/v1/clusters/{cluster}/namespaces        → create a namespace
  PATCH /api/v1/clusters/{cluster}/namespaces/{name}/labels → set / remove labels
  PATCH /api/v1/clusters/{cluster}/namespaces/{name}/annotations → set / remove annotations

All require the ``cluster_api`` scope. Vocabulary follows CONTEXT.md,
"Namespaces". There is deliberately no delete: it takes everything inside the
namespace with it, irreversibly.
"""

from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Path, Query, Request

from app.core.config import get_settings
from app.core.dependencies import get_cluster_repo, get_current_user, get_policy_service
from app.domain.access_policy import PolicyAction, PolicyParam
from app.domain.kubernetes_models import (
    NAMESPACE_NAME_PATTERN,
    NamespaceAnnotationsData,
    NamespaceAnnotationsPatchRequest,
    NamespaceCreateRequest,
    NamespaceDetailData,
    NamespaceLabelsData,
    NamespaceLabelsPatchRequest,
    NamespaceListData,
)
from app.domain.models import ApiResponse, Caller, User
from app.repositories.cluster_repository import ClusterRepository
from app.services.kube_client import call_kube
from app.services.namespace_service import NamespaceService
from app.services.policy_service import PolicyService

router = APIRouter(prefix="/clusters", tags=["namespaces"])

def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


# ── Dependency providers ──────────────────────────────────────────────────────


def _get_namespace_service() -> NamespaceService:
    return NamespaceService(provenance_prefix=get_settings().PROVENANCE_ANNOTATION_PREFIX)


def _caller(request: Request, user: User) -> Caller:
    return Caller(account=user.account, request_id=_request_id(request))


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
    svc: NamespaceService = Depends(_get_namespace_service),
) -> ApiResponse[NamespaceListData]:
    data = await call_kube(
        repo,
        cluster,
        lambda kube: svc.list_namespaces(
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
        "does not exist; a name that is not a DNS-1123 label (e.g. uppercase) "
        "is 422, since no namespace could have it."
    ),
)
async def get_namespace(
    request: Request,
    cluster: str,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    name: str = Path(..., max_length=63, pattern=NAMESPACE_NAME_PATTERN),
    repo: ClusterRepository = Depends(get_cluster_repo),
    svc: NamespaceService = Depends(_get_namespace_service),
) -> ApiResponse[NamespaceDetailData]:
    data = await call_kube(
        repo,
        cluster,
        lambda kube: svc.get_namespace(cluster=cluster, name=name, kube=kube),
    )
    return ApiResponse(data=data, request_id=_request_id(request))


@router.post(
    "/{cluster}/namespaces",
    response_model=ApiResponse[NamespaceDetailData],
    status_code=201,
    summary="Create a namespace",
    description=(
        "Creates a namespace from ``name`` (DNS-1123 label; not ``default``, "
        "not ``kube-*``) and optional ``labels``. A label key under "
        "``kubernetes.io`` / ``k8s.io`` (or a subdomain) is reserved: 422 "
        "naming every such key. Optional ``annotations``: a malformed key, or "
        "one under ``<PROVENANCE_ANNOTATION_PREFIX>``, is 422. Any other field is 422. The service stamps its provenance annotations "
        "(``<PROVENANCE_ANNOTATION_PREFIX>/created-by``, ``/created-at``, "
        "``/last-request-id``): a pointer to the audit trail, not an audit "
        "record (ADR 0002). "
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
    svc: NamespaceService = Depends(_get_namespace_service),
    policy: PolicyService = Depends(get_policy_service),
) -> ApiResponse[NamespaceDetailData]:
    await policy.enforce(
        current_user,
        PolicyAction.NAMESPACE_CREATE,
        {PolicyParam.CLUSTER: cluster, PolicyParam.NAMESPACE: body.name},
    )
    data = await call_kube(
        repo,
        cluster,
        lambda kube: svc.create_namespace(
            cluster=cluster,
            name=body.name,
            labels=body.labels,
            annotations=body.annotations,
            caller=_caller(request, current_user),
            kube=kube,
        ),
    )
    return ApiResponse(data=data, request_id=_request_id(request))


@router.patch(
    "/{cluster}/namespaces/{name}/labels",
    response_model=ApiResponse[NamespaceLabelsData],
    summary="Set or remove namespace labels",
    description=(
        "``set`` adds or overwrites labels, ``remove`` deletes keys (an absent "
        "key is a no-op); labels are never replaced wholesale. The response "
        "holds the namespace's **full current** labels. 422 when: a key is "
        "under ``kubernetes.io`` / ``k8s.io`` (or a subdomain) — in ``set`` or "
        "``remove``, every such key named; a key or value is malformed; both "
        "are empty; a key is in both; or the namespace is ``default`` / "
        "``kube-*``. 404 ``NAMESPACE_NOT_FOUND`` when it does not exist. Also "
        "stamps ``<PROVENANCE_ANNOTATION_PREFIX>/last-modified-by``, "
        "``/last-modified-at`` and ``/last-request-id`` (ADR 0002)."
    ),
)
async def patch_namespace_labels(
    request: Request,
    cluster: str,
    body: NamespaceLabelsPatchRequest,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    name: str = Path(..., max_length=63, pattern=NAMESPACE_NAME_PATTERN),
    repo: ClusterRepository = Depends(get_cluster_repo),
    svc: NamespaceService = Depends(_get_namespace_service),
    policy: PolicyService = Depends(get_policy_service),
) -> ApiResponse[NamespaceLabelsData]:
    await policy.enforce(
        current_user,
        PolicyAction.NAMESPACE_LABEL,
        {PolicyParam.CLUSTER: cluster, PolicyParam.NAMESPACE: name},
    )
    data = await call_kube(
        repo,
        cluster,
        lambda kube: svc.update_labels(
            cluster=cluster,
            name=name,
            set_labels=body.set,
            remove_labels=body.remove,
            caller=_caller(request, current_user),
            kube=kube,
        ),
    )
    return ApiResponse(data=data, request_id=_request_id(request))


@router.patch(
    "/{cluster}/namespaces/{name}/annotations",
    response_model=ApiResponse[NamespaceAnnotationsData],
    summary="Set or remove namespace annotations",
    description=(
        "``set`` adds or overwrites annotations, ``remove`` deletes keys (an "
        "absent key is a no-op); annotations are never replaced wholesale. The "
        "response holds the namespace's **full current** annotations "
        "(``last-applied-configuration`` left out). 422 when: a key is "
        "malformed; a key is under ``<PROVENANCE_ANNOTATION_PREFIX>`` — in "
        "``set`` or ``remove``, since the service alone writes those; both are "
        "empty; a key is in both; or the namespace is ``default`` / "
        "``kube-*``. 404 ``NAMESPACE_NOT_FOUND`` when it does not exist. Also "
        "stamps ``<PROVENANCE_ANNOTATION_PREFIX>/last-modified-by``, "
        "``/last-modified-at`` and ``/last-request-id`` (ADR 0002)."
    ),
)
async def patch_namespace_annotations(
    request: Request,
    cluster: str,
    body: NamespaceAnnotationsPatchRequest,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    name: str = Path(..., max_length=63, pattern=NAMESPACE_NAME_PATTERN),
    repo: ClusterRepository = Depends(get_cluster_repo),
    svc: NamespaceService = Depends(_get_namespace_service),
    policy: PolicyService = Depends(get_policy_service),
) -> ApiResponse[NamespaceAnnotationsData]:
    await policy.enforce(
        current_user,
        PolicyAction.NAMESPACE_ANNOTATE,
        {PolicyParam.CLUSTER: cluster, PolicyParam.NAMESPACE: name},
    )
    data = await call_kube(
        repo,
        cluster,
        lambda kube: svc.update_annotations(
            cluster=cluster,
            name=name,
            set_annotations=body.set,
            remove_annotations=body.remove,
            caller=_caller(request, current_user),
            kube=kube,
        ),
    )
    return ApiResponse(data=data, request_id=_request_id(request))
