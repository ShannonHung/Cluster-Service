"""
app/domain/kubernetes_models.py

Pydantic models for Kubernetes cluster management operations.

Layers:
  - Config models   : KubeClientConfig — carries resolved cluster credentials
  - Request models  : DrainOptions, DrainRequest, NodePatchRequest
  - Response models : NodeActionData, DrainActionData, NodeInfo, NodeListData, …

Response convention:
  Success → ApiResponse[T] → {"data": <T>, "request_id": "..."}
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ──────────────────────────────────────────────────────────────────────────────
# Cluster config — passed between Repository → Factory
# ──────────────────────────────────────────────────────────────────────────────

class KubeClientConfig(BaseModel):
    """Resolved cluster credentials, normalised to a single shape.

    The repository layer produces this object; the KubeClientFactory
    consumes it to build an ApiClient.  Callers never need to know which
    backing store was used.
    """

    cluster_name: str
    source: Literal["yaml", "json", "api"]
    # ── YAML path auth ─────────────────────────────────────────────────────────
    kubeconfig_path: Optional[Path] = None
    # ── Token auth (JSON / API-sourced) ────────────────────────────────────────
    server: Optional[str] = None
    ca_data: Optional[str] = None    # base64-encoded PEM CA certificate
    token: Optional[str] = None      # bearer token

    model_config = ConfigDict(arbitrary_types_allowed=True)


# ──────────────────────────────────────────────────────────────────────────────
# Request models
# ──────────────────────────────────────────────────────────────────────────────

class DrainOptions(BaseModel):
    """Maps 1-to-1 onto ``kubectl drain`` flags.

    Note: ``ignore_daemonsets`` is intentionally absent.  DaemonSet pods are
    **always** skipped — this protection cannot be disabled via the API.
    """

    delete_emptydir_data: bool = Field(
        default=False,
        description="Pass --delete-emptydir-data; remove pods using emptyDir volumes.",
    )
    force: bool = Field(
        default=False,
        description="Pass --force; delete pods not managed by a controller.",
    )
    disable_eviction: bool = Field(
        default=False,
        description=(
            "Bypass PDB by using Delete instead of Eviction API. "
            "Equivalent to --disable-eviction."
        ),
    )
    grace_period_seconds: Optional[int] = Field(
        default=None,
        description="Override pod termination grace period (--grace-period). "
                    "None means use each pod's own setting.",
    )
    timeout_seconds: int = Field(
        default=300,
        ge=1,
        description="Total time to wait for all pods to be deleted (--timeout).",
    )


class DrainRequest(BaseModel):
    """HTTP request body for POST …/drain."""

    options: DrainOptions = Field(
        default_factory=DrainOptions,
        description="Fine-grained drain behaviour flags.",
    )
    dry_run: bool = Field(
        default=False,
        description="Validate without performing any changes.",
    )
    reason: Optional[str] = Field(
        default=None,
        description="Human-readable reason for the drain (logged, not sent to K8s).",
    )


class NodePatchRequest(BaseModel):
    """HTTP request body for PATCH …/labels and PATCH …/annotations.

    ``set``    — key-value pairs to add or overwrite.
    ``remove`` — keys whose values will be nulled (Kubernetes deletion pattern).
    """

    set: dict[str, str] = Field(default_factory=dict, description="Labels/annotations to add or overwrite.")
    remove: list[str] = Field(default_factory=list, description="Label/annotation keys to delete.")


# ──────────────────────────────────────────────────────────────────────────────
# Response / domain models
# ──────────────────────────────────────────────────────────────────────────────

class NodeActionData(BaseModel):
    """Unified response body for cordon / uncordon actions."""

    status: str = "success"
    cluster: str
    node: str
    action: str  # "cordon" | "uncordon"
    dry_run: bool = False


class DrainedPodInfo(BaseModel):
    """Identifies a single pod that was evicted/deleted during drain."""

    name: str
    namespace: str


class DrainActionData(BaseModel):
    """Response body for drain — superset of NodeActionData with pod list."""

    status: str = "success"
    cluster: str
    node: str
    action: str = "drain"
    dry_run: bool = False
    drained_pods: list[DrainedPodInfo] = Field(
        default_factory=list,
        description="Pods that were evicted or deleted during this drain operation.",
    )


class NodeLabelsData(BaseModel):
    """Response for PATCH /labels — the node's current labels after the patch."""

    status: str = "success"
    cluster: str
    node: str
    action: str = "label"
    labels: dict[str, str] = Field(default_factory=dict)


class NodeAnnotationsData(BaseModel):
    """Response for PATCH /annotations — the node's current annotations after the patch."""

    status: str = "success"
    cluster: str
    node: str
    action: str = "annotate"
    annotations: dict[str, str] = Field(default_factory=dict)


class TaintSpec(BaseModel):
    """A single node taint (set form). ``(key, effect)`` is the unique key."""

    key: str
    value: Optional[str] = None
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"]


class TaintRemoveSpec(BaseModel):
    """Identifies a taint to remove by its unique ``(key, effect)``."""

    key: str
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"]


class NodeTaintRequest(BaseModel):
    """HTTP request body for PATCH …/taints."""

    set: list[TaintSpec] = Field(default_factory=list, description="Taints to add or overwrite.")
    remove: list[TaintRemoveSpec] = Field(default_factory=list, description="Taints to remove by key+effect.")


class NodeTaintData(BaseModel):
    """Response for PATCH …/taints — the node's current taints after the patch."""

    status: str = "success"
    cluster: str
    node: str
    action: str = "taint"
    taints: list[TaintSpec] = Field(default_factory=list)


class NodeCondition(BaseModel):
    """Summarised condition entry for a node."""

    type: str
    status: str


class NodeInfo(BaseModel):
    """A single Kubernetes node's key attributes (used in list view)."""

    name: str
    status: str                          # "Ready" | "NotReady" | "Unknown"
    roles: list[str] = Field(default_factory=list)
    version: str = ""                    # kubelet version
    unschedulable: bool = False          # True when cordoned
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)


class NodeListData(BaseModel):
    """Response body for GET …/{cluster}/nodes."""

    cluster: str
    nodes: list[NodeInfo]


class PodInfo(BaseModel):
    """Summary of a pod running on a node."""

    name: str
    namespace: str
    phase: str                        # Running | Pending | Succeeded | Failed | Unknown
    ready: bool = False               # True when all containers are Ready
    owner_kind: Optional[str] = None  # ReplicaSet | DaemonSet | StatefulSet | Job | None
    restart_count: int = 0            # sum of restarts across all containers
    node_name: str = ""               # node the pod is scheduled on (spec.nodeName)


class PodListData(BaseModel):
    """Response body for GET /api/v1/clusters/{cluster}/pods."""

    cluster: str
    namespace: str
    pods: list[PodInfo] = Field(default_factory=list)


class NodeDetailData(BaseModel):
    """Full node detail (node attributes only; pods are queried separately).

    Used by GET …/{cluster}/nodes/{node}.
    """

    cluster: str
    name: str
    status: str
    roles: list[str] = Field(default_factory=list)
    version: str = ""
    unschedulable: bool = False
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    taints: list[TaintSpec] = Field(default_factory=list)


class ClusterInfo(BaseModel):
    """Metadata about a registered cluster."""

    name: str
    source: str = ""  # "yaml" | "json"


class ClusterListData(BaseModel):
    """Response body for GET /clusters."""

    clusters: list[ClusterInfo]
