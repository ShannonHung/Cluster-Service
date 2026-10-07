"""
tests/unit/test_dry_run_kube_client.py

T10: the fake CoreV1Api must satisfy exactly the surface NodeService touches,
with enough fidelity that NodeService's real logic reaches real conclusions.

The subtle requirement is **mutability**. Drain evicts pods and then polls
``list_pod_for_all_namespaces`` until they are gone (``_wait_for_pods_gone``),
so a fake that returned a fixed pod list would spend the entire 25-second wait
budget and report ``still_terminating`` on what should be a clean drain — a slow
false failure rather than an obvious error.

Design shared with deploy-service; see that repo's docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import pytest
from kubernetes.client.exceptions import ApiException

from app.services.dry_run_kube_client import (
    DryRunCoreV1Api,
    reset_dry_run_clusters,
)


_CLUSTER = "unit-test-cluster"


@pytest.fixture(autouse=True)
def _clean_state():
    """Cluster state is process-global, so it must be cleared on both sides or
    one test's cordon changes another's starting conditions."""
    reset_dry_run_clusters()
    yield
    reset_dry_run_clusters()


@pytest.fixture
def kube() -> DryRunCoreV1Api:
    return DryRunCoreV1Api(_CLUSTER)


# ── surface ───────────────────────────────────────────────────────────────────


def test_exposes_every_method_node_service_calls(kube):
    """Missing one does not fail cleanly — it surfaces as an AttributeError
    from inside a worker thread, far from the cause."""
    for name in (
        "list_node",
        "read_node",
        "patch_node",
        "list_pod_for_all_namespaces",
        "list_namespaced_pod",
        "delete_namespaced_pod",
        "create_namespaced_pod_eviction",
    ):
        assert hasattr(kube, name), f"missing {name}"


def test_nodes_carry_the_fields_the_service_reads(kube):
    node = kube.read_node("dry-run-node-ready")
    assert node.metadata.name
    assert node.metadata.labels is not None
    assert node.spec.unschedulable is False
    assert node.status.node_info.kubelet_version
    assert node.status.conditions[0].type == "Ready"


def test_unknown_node_raises_the_sdk_404(kube):
    """NodeService maps a 404 ApiException to NodeNotFoundException. Raising
    anything else would bypass that mapping and surface as a 500."""
    with pytest.raises(ApiException) as exc:
        kube.read_node("no-such-node")
    assert exc.value.status == 404


# ── readiness states ──────────────────────────────────────────────────────────


def test_all_three_readiness_states_are_represented(kube):
    """The uncordon gate is an allowlist: only "True" passes. Covering only a
    Ready node would leave the branch that matters untested, and "Unknown" is a
    distinct third state that a denylist-style check would wrongly admit."""
    statuses = {
        n.metadata.name: n.status.conditions[0].status
        for n in kube.list_node().items
    }
    assert set(statuses.values()) == {"True", "False", "Unknown"}


# ── mutability ────────────────────────────────────────────────────────────────


def test_patch_is_visible_to_the_next_read(kube):
    """A cordon must be reflected in what the API returns next, so the response
    describes the change the caller just made."""
    kube.patch_node("dry-run-node-ready", {"spec": {"unschedulable": True}})
    assert kube.read_node("dry-run-node-ready").spec.unschedulable is True


def test_patch_merges_labels_rather_than_replacing(kube):
    kube.patch_node("dry-run-node-ready", {"metadata": {"labels": {"added": "yes"}}})
    labels = kube.read_node("dry-run-node-ready").metadata.labels
    assert labels["added"] == "yes"
    assert "kubernetes.io/hostname" in labels, "existing labels were dropped"


def test_a_null_label_value_removes_the_key(kube):
    """Kubernetes merge-patch semantics: null deletes. Storing None instead
    would leave the key present with a null value."""
    kube.patch_node("dry-run-node-ready", {"metadata": {"labels": {"dry-run": None}}})
    assert "dry-run" not in kube.read_node("dry-run-node-ready").metadata.labels


def test_eviction_removes_the_pod(kube):
    """Load-bearing for drain: _wait_for_pods_gone polls until the targeted
    pods disappear. A fake that kept them would burn the full wait budget."""
    before = len(kube.list_pod_for_all_namespaces().items)
    kube.create_namespaced_pod_eviction(name="dry-run-web-1", namespace="default")
    after = kube.list_pod_for_all_namespaces().items
    assert len(after) == before - 1
    assert all(p.metadata.name != "dry-run-web-1" for p in after)


def test_delete_removes_the_pod(kube):
    """The disable_eviction=true path bypasses PDBs with a raw delete."""
    kube.delete_namespaced_pod(name="dry-run-web-1", namespace="default")
    assert all(
        p.metadata.name != "dry-run-web-1"
        for p in kube.list_pod_for_all_namespaces().items
    )


def test_state_persists_across_client_instances_for_one_cluster(kube):
    """A real cluster remembers what you did to it.

    The factory builds a fresh *client* per request, so two clients viewing the
    same cluster must see the same state — otherwise a cordon would silently
    revert between requests and an e2e test could not assert its own effect.
    """
    kube.patch_node("dry-run-node-ready", {"spec": {"unschedulable": True}})

    another_client = DryRunCoreV1Api(_CLUSTER)
    assert another_client.read_node("dry-run-node-ready").spec.unschedulable is True


def test_separate_clusters_do_not_share_state(kube):
    """The real factory's isolation guarantee exists to stop cross-cluster
    contamination under concurrency; keying state by cluster preserves it."""
    kube.patch_node("dry-run-node-ready", {"spec": {"unschedulable": True}})

    other_cluster = DryRunCoreV1Api("a-different-cluster")
    assert other_cluster.read_node("dry-run-node-ready").spec.unschedulable is False


def test_reset_clears_state(kube):
    """Process-global state leaks between tests unless it is cleared."""
    kube.patch_node("dry-run-node-ready", {"spec": {"unschedulable": True}})
    reset_dry_run_clusters()
    assert kube.read_node("dry-run-node-ready").spec.unschedulable is False


# ── field selector ────────────────────────────────────────────────────────────


def test_node_name_selector_is_honoured(kube):
    """Drain lists pods for one node. Ignoring the selector would hand back
    another node's pods and the drain would try to evict them."""
    items = kube.list_pod_for_all_namespaces(
        field_selector="spec.nodeName=dry-run-node-ready"
    ).items
    assert items
    assert all(p.spec.node_name == "dry-run-node-ready" for p in items)


def test_a_pod_on_another_node_is_excluded(kube):
    names = {
        p.metadata.name
        for p in kube.list_pod_for_all_namespaces(
            field_selector="spec.nodeName=dry-run-node-ready"
        ).items
    }
    assert "dry-run-other-node" not in names


def test_listing_without_a_selector_returns_everything(kube):
    assert len(kube.list_pod_for_all_namespaces().items) > 1


# ── pod categories drain must distinguish ─────────────────────────────────────


def test_the_always_skipped_categories_are_present(kube):
    """DaemonSet, mirror and completed pods are skipped unconditionally. If the
    fixture lacked them the skip logic would never execute."""
    pods = {p.metadata.name: p for p in kube.list_pod_for_all_namespaces().items}

    daemon = pods["dry-run-daemon"]
    assert [o.kind for o in daemon.metadata.owner_references] == ["DaemonSet"]

    mirror = pods["dry-run-mirror"]
    assert "kubernetes.io/config.mirror" in mirror.metadata.annotations

    assert pods["dry-run-completed"].status.phase == "Succeeded"


def test_the_blocked_categories_are_present(kube):
    """Unmanaged and emptyDir pods make drain refuse before evicting anything.
    Without them, refuse-before-evict — the most important guard in drain —
    could never fire in dry-run."""
    pods = {p.metadata.name: p for p in kube.list_pod_for_all_namespaces().items}

    assert not pods["dry-run-unmanaged"].metadata.owner_references

    volumes = pods["dry-run-emptydir"].spec.volumes
    assert any(v.empty_dir is not None for v in volumes)


def test_an_ordinary_evictable_pod_is_present(kube):
    """The mirror case: if every pod were skipped or blocked, a drain could
    never succeed and the happy path would go untested."""
    pods = {p.metadata.name: p for p in kube.list_pod_for_all_namespaces().items}
    web = pods["dry-run-web-1"]
    assert [o.kind for o in web.metadata.owner_references] == ["ReplicaSet"]
    assert web.status.phase == "Running"
    assert not any(v.empty_dir is not None for v in (web.spec.volumes or []))


# ── configmaps ────────────────────────────────────────────────────────────────

_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def _configmaps(kube) -> dict[tuple[str, str], object]:
    return {
        (cm.metadata.namespace, cm.metadata.name): cm
        for cm in kube.list_config_map_for_all_namespaces().items
    }


def test_exposes_every_method_configmap_service_calls(kube):
    for name in ("list_namespaced_config_map", "list_config_map_for_all_namespaces"):
        assert hasattr(kube, name), f"missing {name}"


def test_namespaced_listing_is_scoped_to_the_namespace(kube):
    items = kube.list_namespaced_config_map("apps").items
    assert items, "an empty namespace proves nothing"
    assert {cm.metadata.namespace for cm in items} == {"apps"}


def test_an_unknown_namespace_lists_nothing(kube):
    """A real cluster answers 200 with an empty list, not 404."""
    assert kube.list_namespaced_config_map("no-such-namespace").items == []


def test_a_data_only_configmap_is_present(kube):
    cm = _configmaps(kube)[("default", "dry-run-app-config")]
    assert cm.data and not cm.binary_data


def test_a_binary_data_configmap_is_present(kube):
    cm = _configmaps(kube)[("default", "dry-run-binary")]
    assert cm.binary_data


def test_a_configmap_carrying_last_applied_is_present(kube):
    """Without it, nothing proves the listing withholds annotations — or, for
    content reads, that this one is stripped while the others survive."""
    cm = _configmaps(kube)[("default", "dry-run-applied")]
    assert _LAST_APPLIED in cm.metadata.annotations
    assert len(cm.metadata.annotations) > 1, "needs a second annotation to survive stripping"


def test_the_same_name_exists_in_two_namespaces_with_different_data(kube):
    """Same name, two namespaces, two different ConfigMaps (CONTEXT.md)."""
    cms = _configmaps(kube)
    a = cms[("default", "dry-run-shared")]
    b = cms[("apps", "dry-run-shared")]
    assert a.data != b.data


def test_listing_hands_out_copies(kube):
    """ConfigMaps are read-only here, so a caller mutating a result must not
    change what the next request sees."""
    kube.list_namespaced_config_map("default").items[0].data["tampered"] = "yes"
    for cm in kube.list_namespaced_config_map("default").items:
        assert "tampered" not in (cm.data or {})
