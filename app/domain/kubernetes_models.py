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

from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.label_rules import check_annotation_keys, check_label_keys, check_labels


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
        ge=0,
        description="Override pod termination grace period (--grace-period). "
                    "None means use each pod's own setting. 0 deletes pods "
                    "immediately without waiting for graceful shutdown — the "
                    "response flags this as a forced deletion.",
    )
    # Note: there is no client-settable drain timeout. The wait budget is owned
    # by the server (DRAIN_DEFAULT_TIMEOUT_SECONDS) and deliberately kept below
    # the front proxy's read timeout, so the app always returns a structured 504
    # rather than a bare proxy 500. On timeout the response lists the pods still
    # running and suggests stronger flags to retry with.


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


class DrainedPodInfo(BaseModel):
    """Identifies a single pod that was evicted/deleted during drain."""

    name: str
    namespace: str


class StillTerminatingPodInfo(BaseModel):
    """A pod whose eviction was accepted but which had not gone by the deadline.

    Not an error: a pod with a long ``terminationGracePeriodSeconds`` is
    shutting down exactly as configured. It is reported so the caller knows
    the node is not yet empty without having to diff two pod listings.
    """

    name: str
    namespace: str


class DrainActionData(BaseModel):
    """Response body for drain — superset of NodeActionData with pod lists."""

    status: str = "success"
    cluster: str
    node: str
    action: str = "drain"
    dry_run: bool = False
    drained_pods: list[DrainedPodInfo] = Field(
        default_factory=list,
        description="Pods whose eviction or deletion this drain requested.",
    )
    still_terminating: list[StillTerminatingPodInfo] = Field(
        default_factory=list,
        description=(
            "Pods still present when the wait budget expired. Empty means the "
            "node is drained. Non-empty is a normal outcome, not a failure — "
            "the drain is idempotent and may be re-run to keep waiting."
        ),
    )
    node_emptied: bool = Field(
        default=True,
        description=(
            "True when every targeted pod is gone. Lets a caller branch on one "
            "field instead of testing a list's length."
        ),
    )
    forced_deletion: bool = Field(
        default=False,
        description=(
            "True when grace_period_seconds=0 was used — pods were killed "
            "immediately with no graceful shutdown."
        ),
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


class ConfigMapSummary(BaseModel):
    """One ConfigMap as a listing shows it: its shape, never its values.

    Annotations are deliberately absent — ``last-applied-configuration`` holds
    a full copy of the values, so passing annotations through would let the
    listing (``cluster_api``) bypass the content privilege. See CONTEXT.md,
    "ConfigMap listing".
    """

    name: str
    namespace: str
    keys: list[str] = Field(
        default_factory=list,
        description="Key names from both data and binaryData, sorted. Never values.",
    )
    labels: dict[str, str] = Field(default_factory=dict)
    creation_timestamp: Optional[datetime] = None


class ConfigMapListData(BaseModel):
    """Response body for GET /api/v1/clusters/{cluster}/configmaps."""

    cluster: str
    namespace: str
    configmaps: list[ConfigMapSummary] = Field(default_factory=list)


class ConfigMapDetailData(BaseModel):
    """Response body for GET …/namespaces/{namespace}/configmaps/{name}.

    The values themselves (CONTEXT.md, "ConfigMap content"). Deliberately not
    the full manifest: ``managedFields`` is bookkeeping noise, and the
    ``last-applied-configuration`` annotation is stripped because it is the
    values as of the last ``kubectl apply`` — after a ``kubectl edit`` it
    disagrees with ``data`` and would read as a second truth.
    """

    cluster: str
    name: str
    namespace: str
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(
        default_factory=dict,
        description="All annotations except kubectl.kubernetes.io/last-applied-configuration.",
    )
    creation_timestamp: Optional[datetime] = None
    data: dict[str, str] = Field(default_factory=dict)
    binary_data: dict[str, str] = Field(
        default_factory=dict,
        description="binaryData as sent by Kubernetes: base64-encoded strings.",
    )


# A Kubernetes namespace name: a DNS-1123 label (at most 63 characters, which
# callers bound with max_length). The one copy every router and model uses.
NAMESPACE_NAME_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"

# Every phase Kubernetes gives a namespace (CONTEXT.md, "Terminating
# namespace"). The API calls it ``status``.
NamespacePhase = Literal["Active", "Terminating"]

# Names that belong to Kubernetes itself (CONTEXT.md, "Reserved namespace
# name"). Fixed, not configurable.
_RESERVED_NAMESPACE = "default"
_RESERVED_NAMESPACE_PREFIX = "kube-"


def is_reserved_namespace_name(name: str) -> bool:
    """CONTEXT.md, Reserved namespace name: `default` and `kube-*`. Fixed."""
    return name == _RESERVED_NAMESPACE or name.startswith(_RESERVED_NAMESPACE_PREFIX)


class NamespaceCreateRequest(BaseModel):
    """Request body for POST /api/v1/clusters/{cluster}/namespaces.

    ``extra="forbid"``: any field but ``name``, ``labels`` and ``annotations`` (a typo
    included) is a 422, never silently dropped. Every check here runs before
    Kubernetes is called: RBAC cannot restrict a ``create`` by name, so this is
    the only place the name is bounded — and the only place a Reserved label
    key (CONTEXT.md) is refused. Annotation keys are shape-checked here; a label
    or annotation key under the provenance prefix is refused in
    ``NamespaceService``, since the prefix is a setting this model cannot see.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        max_length=63,
        pattern=NAMESPACE_NAME_PATTERN,
        description="DNS-1123 label. Not `default`, not starting with `kube-`.",
    )
    labels: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Namespace labels. Keys under `kubernetes.io` / `k8s.io` (or a "
            "subdomain) are reserved and refused."
        ),
    )

    annotations: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Namespace annotations, free-form values. Keys under `kubernetes.io` / "
            "`k8s.io` (or a subdomain) are reserved and refused, as are keys under "
            "the service's provenance prefix: they are the service's own."
        ),
    )

    @field_validator("labels")
    @classmethod
    def _valid_labels(cls, labels: dict[str, str]) -> dict[str, str]:
        check_labels(labels)
        return labels

    @field_validator("annotations")
    @classmethod
    def _valid_annotations(cls, annotations: dict[str, str]) -> dict[str, str]:
        check_annotation_keys(list(annotations))
        return annotations

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, name: str) -> str:
        if is_reserved_namespace_name(name):
            raise ValueError(
                f"'{name}' is reserved for Kubernetes "
                f"('{_RESERVED_NAMESPACE}' and '{_RESERVED_NAMESPACE_PREFIX}*')"
            )
        return name


class NamespaceLabelsPatchRequest(BaseModel):
    """Request body for PATCH /api/v1/clusters/{cluster}/namespaces/{name}/labels.

    Labels change key by key, never wholesale: other parties label namespaces
    too (CONTEXT.md, Namespace label). Every check runs before Kubernetes is
    called; a Reserved label key is refused in ``remove`` as well as ``set``,
    since removing one changes behaviour just as setting it does.
    """

    model_config = ConfigDict(extra="forbid")

    set: dict[str, str] = Field(default_factory=dict, description="Labels to add or overwrite.")
    remove: list[str] = Field(
        default_factory=list, description="Label keys to delete; an absent key is a no-op."
    )

    @field_validator("set")
    @classmethod
    def _valid_set(cls, labels: dict[str, str]) -> dict[str, str]:
        check_labels(labels)
        return labels

    @field_validator("remove")
    @classmethod
    def _valid_remove(cls, keys: list[str]) -> list[str]:
        check_label_keys(keys)
        return keys

    @model_validator(mode="after")
    def _a_change_and_no_contradiction(self) -> "NamespaceLabelsPatchRequest":
        if not self.set and not self.remove:
            raise ValueError("nothing to change: give `set`, `remove`, or both")
        both = sorted(self.set.keys() & set(self.remove))
        if both:
            raise ValueError(f"label keys {both} are in both `set` and `remove`")
        return self


class NamespaceAnnotationsPatchRequest(BaseModel):
    """Request body for PATCH /api/v1/clusters/{cluster}/namespaces/{name}/annotations.

    The counterpart of ``NamespaceLabelsPatchRequest``: annotations change key
    by key, never wholesale, because other parties annotate namespaces too. A
    key under ``kubernetes.io`` / ``k8s.io`` is refused in ``remove`` as well as
    ``set``. The provenance prefix is a setting this model cannot see, so that
    check lives in ``NamespaceService``.
    """

    model_config = ConfigDict(extra="forbid")

    set: dict[str, str] = Field(
        default_factory=dict, description="Annotations to add or overwrite; values are free-form."
    )
    remove: list[str] = Field(
        default_factory=list, description="Annotation keys to delete; an absent key is a no-op."
    )

    @field_validator("set")
    @classmethod
    def _valid_set(cls, annotations: dict[str, str]) -> dict[str, str]:
        check_annotation_keys(list(annotations))
        return annotations

    @field_validator("remove")
    @classmethod
    def _valid_remove(cls, keys: list[str]) -> list[str]:
        check_annotation_keys(keys)
        return keys

    @model_validator(mode="after")
    def _a_change_and_no_contradiction(self) -> "NamespaceAnnotationsPatchRequest":
        if not self.set and not self.remove:
            raise ValueError("nothing to change: give `set`, `remove`, or both")
        both = sorted(self.set.keys() & set(self.remove))
        if both:
            raise ValueError(f"annotation keys {both} are in both `set` and `remove`")
        return self


class NamespaceAnnotationsData(BaseModel):
    """Response body for PATCH …/namespaces/{name}/annotations — the namespace's
    full current annotations after the update, less the stale
    ``last-applied-configuration`` copy a read also leaves out."""

    cluster: str
    name: str
    annotations: dict[str, str] = Field(default_factory=dict)


class NamespaceLabelsData(BaseModel):
    """Response body for PATCH …/namespaces/{name}/labels — the namespace's
    full current labels after the update."""

    cluster: str
    name: str
    labels: dict[str, str] = Field(default_factory=dict)


class NamespaceSummary(BaseModel):
    """One namespace as a listing shows it."""

    name: str
    status: NamespacePhase
    labels: dict[str, str] = Field(default_factory=dict)
    creation_timestamp: Optional[datetime] = None


class NamespaceListData(BaseModel):
    """Response body for GET /api/v1/clusters/{cluster}/namespaces."""

    cluster: str
    namespaces: list[NamespaceSummary] = Field(default_factory=list)


class NamespaceDetailData(BaseModel):
    """Response body for GET /api/v1/clusters/{cluster}/namespaces/{name}.

    ``last-applied-configuration`` is stripped from annotations for the same
    reason as on a ConfigMap: it is a stale copy, not the current state.
    """

    cluster: str
    name: str
    status: NamespacePhase
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(
        default_factory=dict,
        description="All annotations except kubectl.kubernetes.io/last-applied-configuration.",
    )
    creation_timestamp: Optional[datetime] = None
    deletion_timestamp: Optional[datetime] = Field(
        None, description="Set once deletion was requested — i.e. while Terminating."
    )


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


# ──────────────────────────────────────────────────────────────────────────────
# Batch node actions (cordon / uncordon)
# ──────────────────────────────────────────────────────────────────────────────

class BatchNodeRequest(BaseModel):
    """HTTP request body for POST …/nodes:cordon and …/nodes:uncordon."""

    nodes: list[str] = Field(
        min_length=1,
        max_length=100,
        description="Node names to act on. Duplicates are de-duplicated, preserving first-seen order.",
    )
    reason: Optional[str] = Field(
        default=None,
        description="Human-readable reason for the batch (logged, not sent to K8s).",
    )

    @field_validator("nodes")
    @classmethod
    def _strip_node_names(cls, names: list[str]) -> list[str]:
        """Trim surrounding whitespace and drop blanks.

        Kubernetes node names are DNS subdomain names and never contain
        whitespace, so a padded name is a typo. Left as-is it would survive
        de-duplication as a distinct node and come back as its own failure,
        making one mistyped name look like two broken machines.
        """
        stripped = [n.strip() for n in names]
        return [n for n in stripped if n]


class BatchNodeResult(BaseModel):
    """Outcome for a single node in a batch.

    One shape for both outcomes: success entries leave the three error fields
    unset. A success/failure union was rejected — it generates an awkward
    ``anyOf`` in OpenAPI-derived clients.

    A node the access policy refused is an ordinary failure with
    ``error_code`` ``POLICY_DENIED`` (the rule in ``message``) and no
    ``kube_status``: it was never sent to Kubernetes.
    """

    node: str
    status: Literal["success", "failed"]
    error_code: Optional[str] = None
    message: Optional[str] = None
    kube_status: Optional[int] = Field(
        default=None,
        description="Underlying Kubernetes HTTP status — lets callers tell a retryable 503 from a 403.",
    )


class BatchSummary(BaseModel):
    """Aggregate counts for a batch, so callers can branch on one field."""

    total: int
    succeeded: int
    failed: int


class BatchNodeActionData(BaseModel):
    """Response body for the batch cordon / uncordon endpoints.

    ``cluster`` and ``action`` are constant across the batch and hoisted here
    rather than repeated in every result entry.
    """

    cluster: str
    action: str  # "cordon" | "uncordon"
    summary: BatchSummary
    results: list[BatchNodeResult] = Field(default_factory=list)
