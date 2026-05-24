"""
tests/unit/test_node_service.py

Unit tests for NodeService.

CoreV1Api is fully mocked — no Kubernetes cluster required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest
from kubernetes.client.exceptions import ApiException

from app.core.exceptions import KubeApiException, NodeNotFoundException
from app.domain.kubernetes_models import DrainActionData, DrainOptions, NodeActionData, NodeListData, NodeTaintData, PodListData, TaintSpec
from app.services.node_service import NodeService


# ── Helpers ───────────────────────────────────────────────────────────────────

def _svc() -> NodeService:
    """Default service with test-friendly label values."""
    return NodeService(cordon_label_reason="PM", cordon_label_by="infra")


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
    cond.status = "True" if ready else "False"
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
    owner_kind: str = "ReplicaSet",
    is_mirror: bool = False,
    node_name: str = "worker-1",
) -> MagicMock:
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.annotations = {"kubernetes.io/config.mirror": ""} if is_mirror else {}
    owner = MagicMock()
    owner.kind = owner_kind
    pod.metadata.owner_references = [owner]
    pod.status.phase = phase
    pod.status.container_statuses = []
    pod.spec.node_name = node_name
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

def test_cordon_makes_two_patch_calls():
    """First patch: unschedulable=True. Second patch: cordon labels."""
    kube = _make_kube()
    result = _svc().cordon(cluster="test", node_name="worker-1", kube=kube)

    assert kube.patch_node.call_count == 2
    # First call marks unschedulable
    first_body = kube.patch_node.call_args_list[0][0][1]
    assert first_body == {"spec": {"unschedulable": True}}
    # Result
    assert isinstance(result, NodeActionData)
    assert result.action == "cordon"


def test_cordon_applies_configurable_labels():
    kube = _make_kube()
    _svc().cordon(cluster="test", node_name="worker-1", kube=kube)

    second_body = kube.patch_node.call_args_list[1][0][1]
    labels = second_body["metadata"]["labels"]
    assert labels["cordon_reason"] == "PM"
    assert labels["cordon_by"] == "infra"


def test_cordon_custom_label_values():
    """Label values come from the constructor, not hardcoded."""
    svc = NodeService(cordon_label_reason="MAINTENANCE", cordon_label_by="ops-team")
    kube = _make_kube()
    svc.cordon(cluster="test", node_name="n", kube=kube)
    second_body = kube.patch_node.call_args_list[1][0][1]
    labels = second_body["metadata"]["labels"]
    assert labels["cordon_reason"] == "MAINTENANCE"
    assert labels["cordon_by"] == "ops-team"


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
    result = _svc().uncordon(cluster="test", node_name="worker-1", kube=kube)

    first_body = kube.patch_node.call_args_list[0][0][1]
    assert first_body == {"spec": {"unschedulable": False}}
    assert result.action == "uncordon"


def test_uncordon_removes_cordon_labels():
    kube = _make_kube()
    _svc().uncordon(cluster="test", node_name="worker-1", kube=kube)

    # Second patch should null-out both label keys.
    second_body = kube.patch_node.call_args_list[1][0][1]
    labels = second_body["metadata"]["labels"]
    assert labels["cordon_reason"] is None
    assert labels["cordon_by"] is None


def test_uncordon_raises_node_not_found_on_404():
    kube = _make_kube()
    kube.patch_node.side_effect = _api_error(404)
    with pytest.raises(NodeNotFoundException):
        _svc().uncordon(cluster="test", node_name="missing", kube=kube)


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
    assert result.annotations == {"note": "hi"}


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
    assert result.labels == {"env": "prod"}
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
    from app.domain.kubernetes_models import TaintSpec
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
    from app.domain.kubernetes_models import TaintRemoveSpec
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
    from app.domain.kubernetes_models import TaintSpec
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
    from app.domain.kubernetes_models import TaintSpec, TaintRemoveSpec
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
    from app.domain.kubernetes_models import TaintRemoveSpec
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
    from app.domain.kubernetes_models import TaintSpec
    with pytest.raises(NodeNotFoundException):
        _svc().taint_node(
            "test", "missing", kube,
            set_taints=[TaintSpec(key="gpu", effect="NoSchedule")],
            remove_taints=[],
        )
