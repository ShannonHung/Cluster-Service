"""
tests/unit/test_node_service.py

Unit tests for NodeService.

CoreV1Api is fully mocked — no Kubernetes cluster required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app.core.exceptions import (
    DrainBlockedException,
    KubeApiException,
    NodeNotFoundException,
    NodeNotReadyException,
)
from app.domain.kubernetes_models import DrainActionData, DrainOptions, NodeActionData, NodeListData, NodeTaintData, PodListData, TaintRemoveSpec, TaintSpec
from pydantic import ValidationError

from app.services.node_service import NodeService


# ── Helpers ───────────────────────────────────────────────────────────────────

def _svc() -> NodeService:
    return NodeService()


def _make_kube() -> MagicMock:
    return MagicMock()


def _make_node(
    name: str = "worker-1",
    ready: bool = True,
    unschedulable: bool = False,
    kubelet_version: str = "v1.29.0",
    roles: list[str] | None = None,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    taints: list | None = None,
    ready_status: str | None = None,
) -> MagicMock:
    node = MagicMock()
    node.metadata.name = name
    node.metadata.labels = labels or {
        f"node-role.kubernetes.io/{r}": "" for r in (roles or ["worker"])
    }
    node.metadata.annotations = annotations or {}
    node.metadata.owner_references = None
    node.spec.unschedulable = unschedulable
    node.spec.taints = taints if taints is not None else []
    cond = MagicMock()
    cond.type = "Ready"
    # Kubernetes' Ready condition is three-valued: "True", "False", or the
    # literal "Unknown" when the node controller has lost contact with the
    # kubelet. ready_status overrides the boolean to reach that third state.
    cond.status = ready_status if ready_status is not None else ("True" if ready else "False")
    node.status.conditions = [cond]
    node.status.node_info = MagicMock()
    node.status.node_info.kubelet_version = kubelet_version
    return node


def _make_taint(key: str, effect: str, value: str | None = None) -> MagicMock:
    t = MagicMock()
    t.key = key
    t.value = value
    t.effect = effect
    return t


def _make_pod(
    name: str = "mypod",
    namespace: str = "default",
    phase: str = "Running",
    owner_kind: str | None = "ReplicaSet",
    is_mirror: bool = False,
    node_name: str = "worker-1",
    empty_dir: bool = False,
) -> MagicMock:
    """Build a fake V1Pod.

    ``owner_kind=None`` produces an unmanaged (bare) pod — the category
    ``force`` guards. ``empty_dir=True`` attaches an emptyDir volume — the
    category ``delete_emptydir_data`` guards.
    """
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.annotations = {"kubernetes.io/config.mirror": ""} if is_mirror else {}
    if owner_kind is None:
        pod.metadata.owner_references = None
    else:
        owner = MagicMock()
        owner.kind = owner_kind
        pod.metadata.owner_references = [owner]
    pod.status.phase = phase
    pod.status.container_statuses = []
    pod.spec.node_name = node_name

    volume = MagicMock()
    # A non-emptyDir volume must have empty_dir set to None, not a MagicMock —
    # every attribute of a bare MagicMock is truthy, which would make every pod
    # look like an emptyDir user.
    volume.empty_dir = MagicMock() if empty_dir else None
    pod.spec.volumes = [volume]
    return pod


def _api_error(status: int, reason: str = "error") -> ApiException:
    exc = ApiException(status=status, reason=reason)
    exc.status = status
    exc.reason = reason
    return exc


# ── get_node ──────────────────────────────────────────────────────────────────

def test_get_node_returns_detail_without_pods():
    kube = _make_kube()
    kube.read_node.return_value = _make_node("worker-1", labels={"env": "prod"})
    result = _svc().get_node(cluster="test", node_name="worker-1", kube=kube)

    assert result.cluster == "test"
    assert result.name == "worker-1"
    assert result.labels == {"env": "prod"}
    # Node detail must NOT list pods anymore, and must not query pods.
    assert not hasattr(result, "pods")
    kube.list_pod_for_all_namespaces.assert_not_called()


# ── list_nodes ────────────────────────────────────────────────────────────────

def test_list_nodes_returns_node_list():
    kube = _make_kube()
    kube.list_node.return_value.items = [
        _make_node("node-1"),
        _make_node("node-2", ready=False),
    ]
    result = _svc().list_nodes(cluster="test", kube=kube)

    assert isinstance(result, NodeListData)
    assert result.cluster == "test"
    assert len(result.nodes) == 2
    assert result.nodes[0].status == "Ready"
    assert result.nodes[1].status == "NotReady"


def test_list_nodes_includes_labels():
    kube = _make_kube()
    kube.list_node.return_value.items = [
        _make_node("node-1", labels={"env": "prod", "team": "infra"}),
    ]
    result = _svc().list_nodes(cluster="test", kube=kube)
    assert result.nodes[0].labels == {"env": "prod", "team": "infra"}


def test_list_nodes_unschedulable_flag():
    kube = _make_kube()
    kube.list_node.return_value.items = [_make_node("n", unschedulable=True)]
    result = _svc().list_nodes(cluster="test", kube=kube)
    assert result.nodes[0].unschedulable is True


def test_list_nodes_raises_on_api_error():
    kube = _make_kube()
    kube.list_node.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().list_nodes(cluster="test", kube=kube)


# ── cordon ────────────────────────────────────────────────────────────────────

def test_cordon_patches_unschedulable_true():
    kube = _make_kube()
    result = _svc().cordon(cluster="test", node_name="worker-1", kube=kube)

    assert kube.patch_node.call_count == 1
    body = kube.patch_node.call_args_list[0][0][1]
    assert body == {"spec": {"unschedulable": True}}
    assert isinstance(result, NodeActionData)
    assert result.action == "cordon"


def test_cordon_raises_node_not_found_on_404():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(404)
    with pytest.raises(NodeNotFoundException):
        _svc().cordon(cluster="test", node_name="missing", kube=kube)


def test_cordon_raises_kube_api_exception_on_500():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().cordon(cluster="test", node_name="worker", kube=kube)


# ── uncordon ──────────────────────────────────────────────────────────────────

def test_uncordon_patches_unschedulable_false():
    kube = _make_kube()
    kube.read_node.return_value = _make_node("worker-1", ready=True)
    result = _svc().uncordon(cluster="test", node_name="worker-1", kube=kube)

    assert kube.patch_node.call_count == 1
    body = kube.patch_node.call_args_list[0][0][1]
    assert body == {"spec": {"unschedulable": False}}
    assert result.action == "uncordon"


def test_uncordon_raises_node_not_found_on_404():
    """A missing node is a 404, not a 409: the readiness gate is downstream of
    the read, so it never gets the chance to mislabel an absent node."""
    kube = _make_kube()
    kube.read_node.side_effect = _api_error(404)
    with pytest.raises(NodeNotFoundException):
        _svc().uncordon(cluster="test", node_name="missing", kube=kube)


# ── uncordon: readiness gate ─────────────────────────────────────────────────

def test_uncordon_refuses_not_ready_node():
    """Uncordoning declares a node fit for work; a NotReady node is not."""
    kube = _make_kube()
    kube.read_node.return_value = _make_node("worker-1", ready=False)

    with pytest.raises(NodeNotReadyException) as exc_info:
        _svc().uncordon(cluster="test", node_name="worker-1", kube=kube)

    exc = exc_info.value
    assert exc.http_status == 409
    assert exc.detail == {"node": "worker-1", "status": "NotReady"}
    # Nothing was patched — the node keeps whatever schedulability it had.
    kube.patch_node.assert_not_called()


def test_uncordon_refuses_node_whose_kubelet_is_out_of_contact():
    """A silent kubelet is reported by Kubernetes as Ready=Unknown — the
    condition stays present and its status becomes the string "Unknown".

    This is the real-world lost-contact state, and it must be reported as
    Unknown rather than collapsed into NotReady: they have different causes
    and different fixes.
    """
    kube = _make_kube()
    kube.read_node.return_value = _make_node("worker-1", ready_status="Unknown")

    with pytest.raises(NodeNotReadyException) as exc_info:
        _svc().uncordon(cluster="test", node_name="worker-1", kube=kube)

    assert exc_info.value.detail["status"] == "Unknown"
    kube.patch_node.assert_not_called()


def test_uncordon_refuses_node_with_no_conditions_yet():
    """A node that has only just registered has no conditions; its health is
    unestablished, so it is Unknown rather than assumed healthy."""
    kube = _make_kube()
    node = _make_node("worker-1")
    node.status.conditions = []
    kube.read_node.return_value = node

    with pytest.raises(NodeNotReadyException) as exc_info:
        _svc().uncordon(cluster="test", node_name="worker-1", kube=kube)

    assert exc_info.value.detail["status"] == "Unknown"
    kube.patch_node.assert_not_called()


@pytest.mark.parametrize(
    "cond_status, expected",
    [("True", "Ready"), ("False", "NotReady"), ("Unknown", "Unknown")],
)
def test_node_status_maps_all_three_condition_values(cond_status, expected):
    """The Ready condition is three-valued; NotReady and Unknown must not be
    conflated, since only one of them means "nothing is reporting at all"."""
    node = _make_node("worker-1", ready_status=cond_status)
    assert NodeService._node_status(node) == expected


def test_list_nodes_reports_unknown_for_silent_kubelet():
    """The same distinction must survive into the list response, not just the
    uncordon gate — an operator reads it there first."""
    kube = _make_kube()
    kube.list_node.return_value = MagicMock(
        items=[_make_node("worker-1", ready_status="Unknown")]
    )

    result = _svc().list_nodes(cluster="test", kube=kube)

    assert result.nodes[0].status == "Unknown"


def test_cordon_is_not_gated_on_readiness():
    """Cordoning an unhealthy node is exactly what an operator should be able to
    do — the gate belongs on uncordon only."""
    kube = _make_kube()
    kube.read_node.return_value = _make_node("worker-1", ready=False)

    result = _svc().cordon(cluster="test", node_name="worker-1", kube=kube)

    assert result.action == "cordon"
    assert kube.patch_node.call_args_list[0][0][1] == {
        "spec": {"unschedulable": True}
    }


# ── drain ─────────────────────────────────────────────────────────────────────

def test_drain_returns_drain_action_data_with_pod_list():
    kube = _make_kube()
    pod = _make_pod("app-pod", "default", owner_kind="ReplicaSet")
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[pod]),  # listing
        MagicMock(items=[]),     # wait loop
    ]

    result = _svc().drain("test", "worker-1", kube, DrainOptions())

    assert isinstance(result, DrainActionData)
    assert result.action == "drain"
    assert len(result.drained_pods) == 1
    assert result.drained_pods[0].name == "app-pod"
    assert result.drained_pods[0].namespace == "default"


def test_drain_always_skips_daemonset_pods():
    """DaemonSet pods must be skipped regardless of any option."""
    kube = _make_kube()
    ds_pod = _make_pod("ds-pod", owner_kind="DaemonSet")
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[ds_pod]),
        MagicMock(items=[]),
    ]

    result = _svc().drain("test", "worker-1", kube, DrainOptions())

    kube.create_namespaced_pod_eviction.assert_not_called()
    assert len(result.drained_pods) == 0


def test_drain_skips_mirror_pods():
    kube = _make_kube()
    mirror = _make_pod("static", is_mirror=True)
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[mirror]),
        MagicMock(items=[]),
    ]
    result = _svc().drain("test", "worker-1", kube, DrainOptions())
    kube.create_namespaced_pod_eviction.assert_not_called()
    assert len(result.drained_pods) == 0


def test_drain_skips_completed_pods():
    kube = _make_kube()
    done = _make_pod("job-pod", phase="Succeeded")
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[done]),
        MagicMock(items=[]),
    ]
    result = _svc().drain("test", "worker-1", kube, DrainOptions())
    kube.create_namespaced_pod_eviction.assert_not_called()
    assert len(result.drained_pods) == 0


def test_drain_uses_delete_when_disable_eviction():
    kube = _make_kube()
    pod = _make_pod("app-pod")
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[pod]),
        MagicMock(items=[]),
    ]
    _svc().drain("test", "worker-1", kube, DrainOptions(disable_eviction=True))
    kube.delete_namespaced_pod.assert_called_once()
    kube.create_namespaced_pod_eviction.assert_not_called()


def test_drain_raises_node_not_found_when_cordon_fails():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(404)
    with pytest.raises(NodeNotFoundException):
        _svc().drain("test", "missing-node", kube, DrainOptions())


def test_drain_raises_on_pod_list_failure():
    kube = _make_kube()
    kube.patch_node.return_value = MagicMock()
    kube.list_pod_for_all_namespaces.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().drain("test", "worker-1", kube, DrainOptions())


def _timeout_setup(monkeypatch, default: float, pod: MagicMock | None = None) -> MagicMock:
    """Build a kube whose target pod never disappears and freeze the clock so the
    drain deadline (settings-driven) expires on the first wait-loop check."""
    kube = _make_kube()
    stuck = pod if pod is not None else _make_pod("stuck-pod", "default", owner_kind="ReplicaSet")
    kube.list_pod_for_all_namespaces.return_value = MagicMock(items=[stuck])
    monkeypatch.setattr("app.services.node_service.time.sleep", lambda _s: None)
    times = iter([0.0, float(default) + 1.0])  # start, then past-deadline
    monkeypatch.setattr(
        "app.services.node_service.time.monotonic",
        lambda: next(times, 9999.0),
    )
    return kube


# ── drain: slow termination is not an error ───────────────────────────────────

def test_drain_reports_still_terminating_instead_of_raising(monkeypatch):
    """A pod that outlives the wait budget is a normal outcome, not a failure.

    Every eviction was accepted; a long terminationGracePeriodSeconds means the
    pod is shutting down as configured. Drain returns 200 with the leftovers
    named, so the caller sees what is still going without diffing pod listings.
    """
    from app.core.config import get_settings

    get_settings.cache_clear()
    default = get_settings().DRAIN_DEFAULT_TIMEOUT_SECONDS
    kube = _timeout_setup(monkeypatch, default)

    result = _svc().drain("test", "worker-1", kube, DrainOptions())

    assert isinstance(result, DrainActionData)
    assert result.node_emptied is False
    assert [(p.namespace, p.name) for p in result.still_terminating] == [
        ("default", "stuck-pod")
    ]
    # The pod was still targeted — it appears in both lists, which is the point:
    # "we asked it to go" and "it has not gone yet" are different facts.
    assert [p.name for p in result.drained_pods] == ["stuck-pod"]


def test_drain_sets_node_emptied_when_all_pods_gone():
    """The happy path flips node_emptied and leaves still_terminating empty."""
    kube = _make_kube()
    pod = _make_pod("web-1", "default")
    # First call lists the pod, the wait loop then sees an empty node.
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[pod]),
        MagicMock(items=[]),
    ]

    result = _svc().drain("test", "worker-1", kube, DrainOptions())

    assert result.node_emptied is True
    assert result.still_terminating == []
    assert [p.name for p in result.drained_pods] == ["web-1"]


def test_drain_wait_returns_pods_that_vanish_exactly_at_deadline(monkeypatch):
    """A pod gone on the final poll counts as drained, not as still-terminating.

    The deadline is checked after polling, so the last observation wins — a pod
    that terminates in the final second is not misreported as stuck.
    """
    from app.core.config import get_settings

    get_settings.cache_clear()
    default = get_settings().DRAIN_DEFAULT_TIMEOUT_SECONDS
    kube = _make_kube()
    pod = _make_pod("late-pod", "default")
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[pod]),   # initial listing
        MagicMock(items=[]),      # wait loop: already gone
    ]
    monkeypatch.setattr("app.services.node_service.time.sleep", lambda _s: None)
    times = iter([0.0, float(default) + 1.0])
    monkeypatch.setattr(
        "app.services.node_service.time.monotonic", lambda: next(times, 9999.0)
    )

    result = _svc().drain("test", "worker-1", kube, DrainOptions())
    assert result.node_emptied is True


# ── drain: pre-flight blocking (force / delete_emptydir_data) ─────────────────

def _drain_with(pods: list[MagicMock], options: DrainOptions) -> DrainActionData:
    """Run a drain over *pods*, with the wait loop seeing an emptied node."""
    kube = _make_kube()
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=pods),
        MagicMock(items=[]),
    ]
    return _svc().drain("test", "worker-1", kube, options)


def test_drain_blocks_unmanaged_pod_without_force():
    """A pod with no controller has nothing to recreate it — deleting it is
    permanent, so drain refuses until the caller says force=true."""
    bare = _make_pod("bare-pod", "default", owner_kind=None)

    with pytest.raises(DrainBlockedException) as exc_info:
        _drain_with([bare], DrainOptions())

    exc = exc_info.value
    assert exc.http_status == 400
    assert exc.detail["required_options"] == {"force": True}
    assert exc.detail["blocked_pods"] == [
        {"namespace": "default", "name": "bare-pod", "reasons": ["unmanaged"]}
    ]


def test_drain_evicts_unmanaged_pod_with_force():
    bare = _make_pod("bare-pod", "default", owner_kind=None)
    result = _drain_with([bare], DrainOptions(force=True))
    assert [p.name for p in result.drained_pods] == ["bare-pod"]


def test_drain_blocks_emptydir_pod_without_flag():
    """emptyDir dies with the pod, so its data exists nowhere else."""
    pod = _make_pod("cache-1", "default", empty_dir=True)

    with pytest.raises(DrainBlockedException) as exc_info:
        _drain_with([pod], DrainOptions())

    assert exc_info.value.detail["required_options"] == {"delete_emptydir_data": True}


def test_drain_evicts_emptydir_pod_with_flag():
    pod = _make_pod("cache-1", "default", empty_dir=True)
    result = _drain_with([pod], DrainOptions(delete_emptydir_data=True))
    assert [p.name for p in result.drained_pods] == ["cache-1"]


def test_drain_reports_both_reasons_for_doubly_blocked_pod():
    """One pod breaking two rules must report both flags in a single response.

    Reporting only the first would make the caller fix one, retry, and hit the
    other — exactly the slow round-trip discovery this check exists to avoid.
    """
    pod = _make_pod("bare-cache", "default", owner_kind=None, empty_dir=True)

    with pytest.raises(DrainBlockedException) as exc_info:
        _drain_with([pod], DrainOptions())

    exc = exc_info.value
    assert exc.detail["blocked_pods"][0]["reasons"] == ["unmanaged", "emptydir"]
    assert exc.detail["required_options"] == {
        "force": True,
        "delete_emptydir_data": True,
    }


def test_drain_aggregates_required_options_across_pods():
    """Two pods blocked for different reasons still yield one complete answer."""
    bare = _make_pod("bare-pod", "default", owner_kind=None)
    cache = _make_pod("cache-1", "default", empty_dir=True)

    with pytest.raises(DrainBlockedException) as exc_info:
        _drain_with([bare, cache], DrainOptions())

    assert exc_info.value.detail["required_options"] == {
        "force": True,
        "delete_emptydir_data": True,
    }


def test_drain_blocked_evicts_nothing():
    """The refusal must happen before any eviction — a half-drained node is
    worse than an untouched one, since killed pods cannot be recalled."""
    kube = _make_kube()
    bare = _make_pod("bare-pod", "default", owner_kind=None)
    healthy = _make_pod("web-1", "default")
    kube.list_pod_for_all_namespaces.return_value = MagicMock(items=[bare, healthy])

    with pytest.raises(DrainBlockedException):
        _svc().drain("test", "worker-1", kube, DrainOptions())

    kube.create_namespaced_pod_eviction.assert_not_called()
    kube.delete_namespaced_pod.assert_not_called()


def test_drain_blocked_still_cordons():
    """Cordon precedes the check and is not rolled back: it is harmless alone,
    and leaving it set means a corrected retry has nothing to redo."""
    kube = _make_kube()
    kube.list_pod_for_all_namespaces.return_value = MagicMock(
        items=[_make_pod("bare-pod", "default", owner_kind=None)]
    )

    with pytest.raises(DrainBlockedException):
        _svc().drain("test", "worker-1", kube, DrainOptions())

    kube.patch_node.assert_called_once()
    assert kube.patch_node.call_args.args[1]["spec"]["unschedulable"] is True


def test_drain_never_blocks_on_always_skipped_pods():
    """Unconditional skips are evaluated before the opt-out checks, so a
    DaemonSet pod using emptyDir does not demand delete_emptydir_data."""
    pod = _make_pod("ds-1", "kube-system", owner_kind="DaemonSet", empty_dir=True)
    result = _drain_with([pod], DrainOptions())
    assert result.drained_pods == []
    assert result.node_emptied is True


def test_drain_mirror_pod_with_emptydir_is_skipped_not_blocked():
    pod = _make_pod("static-1", "kube-system", is_mirror=True, empty_dir=True)
    result = _drain_with([pod], DrainOptions())
    assert result.drained_pods == []


def test_drain_completed_pod_without_owner_is_skipped_not_blocked():
    """A Succeeded bare pod is already finished — force must not be demanded."""
    pod = _make_pod("job-1", "default", phase="Succeeded", owner_kind=None)
    result = _drain_with([pod], DrainOptions())
    assert result.drained_pods == []


# ── drain: grace_period_seconds ───────────────────────────────────────────────

def test_drain_passes_grace_period_to_eviction():
    pod = _make_pod("web-1", "default")
    kube = _make_kube()
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[pod]),
        MagicMock(items=[]),
    ]
    _svc().drain("test", "worker-1", kube, DrainOptions(grace_period_seconds=30))

    body = kube.create_namespaced_pod_eviction.call_args.kwargs["body"]
    assert body.delete_options.grace_period_seconds == 30


def test_drain_passes_grace_period_to_delete_when_eviction_disabled():
    pod = _make_pod("web-1", "default")
    kube = _make_kube()
    kube.list_pod_for_all_namespaces.side_effect = [
        MagicMock(items=[pod]),
        MagicMock(items=[]),
    ]
    _svc().drain(
        "test", "worker-1", kube,
        DrainOptions(disable_eviction=True, grace_period_seconds=0),
    )
    assert kube.delete_namespaced_pod.call_args.kwargs["grace_period_seconds"] == 0


@pytest.mark.parametrize(
    "grace, expected_forced",
    [(None, False), (0, True), (30, False)],
)
def test_drain_flags_forced_deletion_only_for_grace_zero(grace, expected_forced):
    """grace=0 kills immediately with no graceful shutdown, so the response says
    so. None (use the pod's own setting) and a positive value do not."""
    result = _drain_with(
        [_make_pod("web-1", "default")],
        DrainOptions(grace_period_seconds=grace),
    )
    assert result.forced_deletion is expected_forced


def test_drain_rejects_negative_grace_period():
    """A negative grace period is always a caller bug — reject it in the model
    rather than spending a Kubernetes round-trip to be told the same."""
    with pytest.raises(ValidationError):
        DrainOptions(grace_period_seconds=-1)


# ── drain: full option matrix ─────────────────────────────────────────────────

@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("delete_emptydir_data", [False, True])
@pytest.mark.parametrize("disable_eviction", [False, True])
@pytest.mark.parametrize("grace_period_seconds", [None, 0, 30])
def test_drain_option_matrix(force, delete_emptydir_data, disable_eviction, grace_period_seconds):
    """Every combination of the four flags against one pod of each guarded kind.

    A pod is drained only when its guard is lifted; the drain is refused when
    any guard still applies. disable_eviction and grace_period_seconds change
    *how* pods leave, never *whether* they may — so they must not affect which
    pods are blocked.
    """
    options = DrainOptions(
        force=force,
        delete_emptydir_data=delete_emptydir_data,
        disable_eviction=disable_eviction,
        grace_period_seconds=grace_period_seconds,
    )
    pods = [
        _make_pod("web-1", "default"),                          # always eligible
        _make_pod("bare-pod", "default", owner_kind=None),      # needs force
        _make_pod("cache-1", "default", empty_dir=True),        # needs emptydir
        _make_pod("ds-1", "kube-system", owner_kind="DaemonSet"),  # always skipped
    ]

    expected_required = {}
    if not force:
        expected_required["force"] = True
    if not delete_emptydir_data:
        expected_required["delete_emptydir_data"] = True

    if expected_required:
        with pytest.raises(DrainBlockedException) as exc_info:
            _drain_with(pods, options)
        assert exc_info.value.detail["required_options"] == expected_required
        return

    result = _drain_with(pods, options)
    # DaemonSet is skipped regardless; the other three are all unblocked here.
    assert sorted(p.name for p in result.drained_pods) == ["bare-pod", "cache-1", "web-1"]
    assert result.forced_deletion is (grace_period_seconds == 0)


# ── label_node ────────────────────────────────────────────────────────────────

def test_label_node_calls_patch_with_labels():
    kube = _make_kube()
    # read_node returns current state after patch
    kube.read_node.return_value = _make_node(
        labels={"env": "prod"}, annotations={"note": "hi"}
    )
    result = _svc().label_node("test", "n", kube, set_labels={"env": "prod"})

    kube.patch_node.assert_called_once_with("n", {"metadata": {"labels": {"env": "prod"}}})
    assert result.action == "label"
    assert result.labels == {"env": "prod"}
    assert not hasattr(result, "annotations")


def test_label_node_removes_labels_with_null():
    kube = _make_kube()
    kube.read_node.return_value = _make_node(labels={}, annotations={})
    _svc().label_node("test", "n", kube, remove_labels=["old-key"])

    body = kube.patch_node.call_args[0][1]
    assert body["metadata"]["labels"]["old-key"] is None


def test_label_node_set_and_remove_together():
    kube = _make_kube()
    kube.read_node.return_value = _make_node(labels={"new": "val"}, annotations={})
    _svc().label_node("test", "n", kube, set_labels={"new": "val"}, remove_labels=["old"])

    body = kube.patch_node.call_args[0][1]
    labels = body["metadata"]["labels"]
    assert labels["new"] == "val"
    assert labels["old"] is None


def test_label_node_no_op_when_nothing_provided():
    kube = _make_kube()
    _svc().label_node("test", "n", kube)
    kube.patch_node.assert_not_called()
    kube.read_node.assert_not_called()


# ── annotate_node ─────────────────────────────────────────────────────────────

def test_annotate_node_calls_patch_with_annotations():
    kube = _make_kube()
    kube.read_node.return_value = _make_node(
        labels={"env": "prod"}, annotations={"note": "hello"}
    )
    result = _svc().annotate_node("test", "n", kube, set_annotations={"note": "hello"})

    kube.patch_node.assert_called_once_with(
        "n", {"metadata": {"annotations": {"note": "hello"}}}
    )
    assert result.action == "annotate"
    assert not hasattr(result, "labels")
    assert result.annotations == {"note": "hello"}


def test_annotate_node_removes_with_null():
    kube = _make_kube()
    kube.read_node.return_value = _make_node(labels={}, annotations={})
    _svc().annotate_node("test", "n", kube, remove_annotations=["old"])

    body = kube.patch_node.call_args[0][1]
    assert body["metadata"]["annotations"]["old"] is None


# ── list_pods ─────────────────────────────────────────────────────────────────

def test_list_pods_no_filters_returns_all_in_namespace():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("web-1", node_name="n1"),
        _make_pod("api-1", node_name="n2"),
    ]
    result = _svc().list_pods(cluster="test", namespace="default", kube=kube)

    assert isinstance(result, PodListData)
    assert result.cluster == "test"
    assert result.namespace == "default"
    assert {p.name for p in result.pods} == {"web-1", "api-1"}
    kube.list_namespaced_pod.assert_called_once_with("default")
    kube.list_pod_for_all_namespaces.assert_not_called()


def test_list_pods_wildcard_lists_all_namespaces():
    kube = _make_kube()
    kube.list_pod_for_all_namespaces.return_value.items = [
        _make_pod("web-1", namespace="default", node_name="n1"),
        _make_pod("kube-dns", namespace="kube-system", node_name="n2"),
    ]
    result = _svc().list_pods(cluster="test", namespace="*", kube=kube)

    assert result.namespace == "*"
    assert {p.name for p in result.pods} == {"web-1", "kube-dns"}
    kube.list_pod_for_all_namespaces.assert_called_once_with()
    kube.list_namespaced_pod.assert_not_called()


def test_list_pods_wildcard_still_applies_filters():
    kube = _make_kube()
    kube.list_pod_for_all_namespaces.return_value.items = [
        _make_pod("web-1", namespace="default", node_name="n1", phase="Running"),
        _make_pod("web-2", namespace="kube-system", node_name="n2", phase="Pending"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="*", kube=kube, statuses=["Running"]
    )
    assert {p.name for p in result.pods} == {"web-1"}


def test_list_pods_filters_by_node():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("web-1", node_name="n1"),
        _make_pod("web-2", node_name="n2"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, nodes=["n1"]
    )
    assert {p.name for p in result.pods} == {"web-1"}


def test_list_pods_filters_by_multiple_nodes_or():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("a", node_name="n1"),
        _make_pod("b", node_name="n2"),
        _make_pod("c", node_name="n3"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, nodes=["n1", "n2"]
    )
    assert {p.name for p in result.pods} == {"a", "b"}


def test_list_pods_filters_by_status_case_insensitive():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("running-pod", phase="Running"),
        _make_pod("pending-pod", phase="Pending"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, statuses=["running"]
    )
    assert {p.name for p in result.pods} == {"running-pod"}


def test_list_pods_filters_by_name_prefix_or():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("web-7d9f"),
        _make_pod("api-xyz"),
        _make_pod("db-1"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, name_prefixes=["web-", "api-"]
    )
    assert {p.name for p in result.pods} == {"web-7d9f", "api-xyz"}


def test_list_pods_filters_are_anded():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("web-1", node_name="n1", phase="Running"),
        _make_pod("web-2", node_name="n2", phase="Running"),
        _make_pod("web-3", node_name="n1", phase="Pending"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube,
        nodes=["n1"], statuses=["Running"], name_prefixes=["web-"],
    )
    assert {p.name for p in result.pods} == {"web-1"}


def test_list_pods_empty_when_no_match():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [_make_pod("web-1", node_name="n1")]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, nodes=["nonexistent"]
    )
    assert result.pods == []


def test_list_pods_raises_on_api_error():
    kube = _make_kube()
    kube.list_namespaced_pod.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().list_pods(cluster="test", namespace="default", kube=kube)


def test_list_pods_wildcard_raises_on_api_error():
    kube = _make_kube()
    kube.list_pod_for_all_namespaces.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().list_pods(cluster="test", namespace="*", kube=kube)


# ── taint_node ────────────────────────────────────────────────────────────────

def test_taint_node_adds_new_taint():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[]),
        _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "true")]),
    ]
    result = _svc().taint_node(
        "test", "n", kube,
        set_taints=[TaintSpec(key="gpu", value="true", effect="NoSchedule")],
        remove_taints=[],
    )
    body = kube.patch_node.call_args[0][1]
    taints = body["spec"]["taints"]
    assert {(t["key"], t["effect"], t.get("value")) for t in taints} == {("gpu", "NoSchedule", "true")}
    assert result.action == "taint"
    assert any(t.key == "gpu" and t.effect == "NoSchedule" for t in result.taints)


def test_taint_node_removes_by_key_and_effect():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "true")]),
        _make_node("n", taints=[]),
    ]
    result = _svc().taint_node(
        "test", "n", kube,
        set_taints=[],
        remove_taints=[TaintRemoveSpec(key="gpu", effect="NoSchedule")],
    )
    body = kube.patch_node.call_args[0][1]
    assert body["spec"]["taints"] == []
    assert result.taints == []


def test_taint_node_same_key_effect_overwrites_value():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "old")]),
        _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "new")]),
    ]
    _svc().taint_node(
        "test", "n", kube,
        set_taints=[TaintSpec(key="gpu", value="new", effect="NoSchedule")],
        remove_taints=[],
    )
    body = kube.patch_node.call_args[0][1]
    taints = body["spec"]["taints"]
    matching = [t for t in taints if t["key"] == "gpu" and t["effect"] == "NoSchedule"]
    assert len(matching) == 1
    assert matching[0]["value"] == "new"


def test_taint_node_set_and_remove_together():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[_make_taint("old", "NoSchedule", None)]),
        _make_node("n", taints=[_make_taint("new", "NoExecute", "1")]),
    ]
    _svc().taint_node(
        "test", "n", kube,
        set_taints=[TaintSpec(key="new", value="1", effect="NoExecute")],
        remove_taints=[TaintRemoveSpec(key="old", effect="NoSchedule")],
    )
    body = kube.patch_node.call_args[0][1]
    keys = {(t["key"], t["effect"]) for t in body["spec"]["taints"]}
    assert keys == {("new", "NoExecute")}


def test_taint_node_remove_nonexistent_is_noop_not_error():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[_make_taint("keep", "NoSchedule", None)]),
        _make_node("n", taints=[_make_taint("keep", "NoSchedule", None)]),
    ]
    result = _svc().taint_node(
        "test", "n", kube,
        set_taints=[],
        remove_taints=[TaintRemoveSpec(key="ghost", effect="NoExecute")],
    )
    body = kube.patch_node.call_args[0][1]
    keys = {(t["key"], t["effect"]) for t in body["spec"]["taints"]}
    assert keys == {("keep", "NoSchedule")}
    assert any(t.key == "keep" for t in result.taints)


def test_taint_node_no_op_when_nothing_provided():
    kube = _make_kube()
    kube.read_node.return_value = _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "true")])
    result = _svc().taint_node("test", "n", kube, set_taints=[], remove_taints=[])
    kube.patch_node.assert_not_called()
    assert any(t.key == "gpu" for t in result.taints)


def test_taint_node_raises_node_not_found_on_404():
    kube = _make_kube()
    kube.read_node.side_effect = _api_error(404)
    with pytest.raises(NodeNotFoundException):
        _svc().taint_node(
            "test", "missing", kube,
            set_taints=[TaintSpec(key="gpu", effect="NoSchedule")],
            remove_taints=[],
        )


def test_taint_node_raises_kube_api_exception_on_read_500():
    kube = _make_kube()
    kube.read_node.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().taint_node(
            "test", "n", kube,
            set_taints=[TaintSpec(key="gpu", effect="NoSchedule")],
            remove_taints=[],
        )


def test_taint_node_raises_kube_api_exception_on_patch_500():
    kube = _make_kube()
    kube.read_node.return_value = _make_node("n", taints=[])
    kube.patch_node.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().taint_node(
            "test", "n", kube,
            set_taints=[TaintSpec(key="gpu", effect="NoSchedule")],
            remove_taints=[],
        )


# ── cordon_many / uncordon_many ───────────────────────────────────────────────

def test_cordon_many_all_succeed():
    kube = _make_kube()
    result = _svc().cordon_many(cluster="test", node_names=["n1", "n2"], kube=kube)

    assert result.cluster == "test"
    assert result.action == "cordon"
    assert result.summary.total == 2
    assert result.summary.succeeded == 2
    assert result.summary.failed == 0
    assert [r.node for r in result.results] == ["n1", "n2"]
    assert all(r.status == "success" for r in result.results)


def test_cordon_many_partial_failure_keeps_going():
    kube = _make_kube()
    kube.patch_node.side_effect = [None, _api_error(404, "Not Found"), None]

    result = _svc().cordon_many(cluster="test", node_names=["n1", "n2", "n3"], kube=kube)

    assert result.summary.total == 3
    assert result.summary.succeeded == 2
    assert result.summary.failed == 1

    failed = [r for r in result.results if r.status == "failed"]
    assert len(failed) == 1
    assert failed[0].node == "n2"
    assert failed[0].error_code == "NODE_NOT_FOUND"
    assert failed[0].kube_status == 404
    assert "n2" in failed[0].message

    # The node after the failure was still attempted.
    assert [r.node for r in result.results] == ["n1", "n2", "n3"]
    assert result.results[2].status == "success"


def test_cordon_many_api_error_carries_underlying_status():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(503, "Service Unavailable")

    result = _svc().cordon_many(cluster="test", node_names=["n1"], kube=kube)

    assert result.summary.failed == 1
    assert result.results[0].error_code == "KUBE_API_ERROR"
    assert result.results[0].kube_status == 503


def test_cordon_many_reports_no_status_when_the_api_gave_none():
    """An ApiException without a status must not crash the batch, and the
    per-node result must not invent one: kube_status is what the API server
    said, and here it said nothing."""
    kube = _make_kube()
    kube.patch_node.side_effect = ApiException(reason="no response")

    result = _svc().cordon_many(cluster="test", node_names=["n1"], kube=kube)

    assert result.summary.failed == 1
    assert result.results[0].error_code == "KUBE_API_ERROR"
    assert result.results[0].kube_status is None


def test_read_with_no_status_is_a_502_kube_api_error():
    kube = _make_kube()
    kube.read_node.side_effect = ApiException(reason="no response")

    with pytest.raises(KubeApiException) as exc_info:
        _svc().get_node(cluster="test", node_name="n1", kube=kube)
    assert exc_info.value.http_status == 502
    assert exc_info.value.kube_status is None


def test_cordon_many_deduplicates_node_names():
    kube = _make_kube()

    result = _svc().cordon_many(cluster="test", node_names=["n1", "n2", "n1"], kube=kube)

    assert result.summary.total == 2
    assert [r.node for r in result.results] == ["n1", "n2"]
    assert kube.patch_node.call_count == 2


def _stub_node_statuses(kube: MagicMock, **ready_by_name: bool) -> None:
    """Make ``list_node`` report the given nodes with the given readiness."""
    kube.list_node.return_value = MagicMock(
        items=[_make_node(name, ready=ready) for name, ready in ready_by_name.items()]
    )


def test_uncordon_many_patches_unschedulable_false():
    kube = _make_kube()
    _stub_node_statuses(kube, n1=True, n2=True)

    result = _svc().uncordon_many(cluster="test", node_names=["n1", "n2"], kube=kube)

    assert result.action == "uncordon"
    assert result.summary.succeeded == 2
    kube.patch_node.assert_has_calls([
        call("n1", {"spec": {"unschedulable": False}}),
        call("n2", {"spec": {"unschedulable": False}}),
    ])


def test_uncordon_many_reads_readiness_once_for_the_whole_batch():
    """One listing, not one read per node — the cost of the gate must not scale
    with batch size."""
    kube = _make_kube()
    _stub_node_statuses(kube, n1=True, n2=True, n3=True)

    _svc().uncordon_many(cluster="test", node_names=["n1", "n2", "n3"], kube=kube)

    assert kube.list_node.call_count == 1
    kube.read_node.assert_not_called()


def test_uncordon_many_fails_only_the_not_ready_nodes():
    """A NotReady node fails on its own and never aborts the batch."""
    kube = _make_kube()
    _stub_node_statuses(kube, n1=True, n2=False, n3=True)

    result = _svc().uncordon_many(
        cluster="test", node_names=["n1", "n2", "n3"], kube=kube,
    )

    assert result.summary.succeeded == 2
    assert result.summary.failed == 1
    failed = [r for r in result.results if r.status == "failed"]
    assert [r.node for r in failed] == ["n2"]
    assert failed[0].error_code == "NODE_NOT_READY"
    assert failed[0].kube_status == 409
    # The healthy nodes were still patched.
    kube.patch_node.assert_has_calls([
        call("n1", {"spec": {"unschedulable": False}}),
        call("n3", {"spec": {"unschedulable": False}}),
    ])


def test_uncordon_many_all_not_ready_is_still_per_node():
    """Every node failing looks like a cluster problem but is not evidence of
    one — the batch must not promote a guess into a propagated exception."""
    kube = _make_kube()
    _stub_node_statuses(kube, n1=False, n2=False)

    result = _svc().uncordon_many(cluster="test", node_names=["n1", "n2"], kube=kube)

    assert result.summary.failed == 2
    assert result.summary.succeeded == 0
    assert all(r.error_code == "NODE_NOT_READY" for r in result.results)
    kube.patch_node.assert_not_called()


def test_uncordon_many_node_absent_from_listing_yields_404_not_409():
    """A node missing from the listing is gone, not unhealthy. Letting the patch
    run produces the real 404 instead of inventing a readiness verdict."""
    kube = _make_kube()
    _stub_node_statuses(kube, n1=True)
    kube.patch_node.side_effect = _api_error(404)

    result = _svc().uncordon_many(cluster="test", node_names=["ghost"], kube=kube)

    assert result.summary.failed == 1
    assert result.results[0].error_code == "NODE_NOT_FOUND"


def test_uncordon_many_with_no_nodes_skips_the_listing():
    """An all-blank body survives min_length validation and arrives empty; it
    must not pay for a cluster listing to iterate zero nodes."""
    kube = _make_kube()

    result = _svc().uncordon_many(cluster="test", node_names=[], kube=kube)

    assert result.summary.total == 0
    kube.list_node.assert_not_called()


def test_cordon_many_does_not_check_readiness():
    """Cordoning is how an operator responds to a sick node — never gated, and
    it must not pay for a listing it does not use."""
    kube = _make_kube()

    result = _svc().cordon_many(cluster="test", node_names=["n1", "n2"], kube=kube)

    assert result.summary.succeeded == 2
    kube.list_node.assert_not_called()


def test_cordon_many_patches_unschedulable_true():
    kube = _make_kube()

    _svc().cordon_many(cluster="test", node_names=["n1"], kube=kube)

    kube.patch_node.assert_called_once_with("n1", {"spec": {"unschedulable": True}})


def test_cordon_many_propagates_cluster_connection_failure():
    """A cluster-level failure must not be downgraded to N per-node failures."""
    kube = _make_kube()
    kube.patch_node.side_effect = Urllib3HTTPError("connection refused")

    with pytest.raises(KubeApiException):
        _svc().cordon_many(cluster="test", node_names=["n1", "n2"], kube=kube)


@pytest.mark.parametrize("status", [401, 403])
def test_cordon_many_propagates_auth_failure(status):
    """Bad credentials are a cluster-level failure, not N broken nodes."""
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(status, "Unauthorized")

    with pytest.raises(KubeApiException):
        _svc().cordon_many(cluster="test", node_names=["n1", "n2"], kube=kube)
