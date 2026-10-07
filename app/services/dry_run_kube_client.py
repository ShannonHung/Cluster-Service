"""
app/services/dry_run_kube_client.py

A stand-in for ``kubernetes.client.CoreV1Api`` used when ``DRY_RUN_MODE=true``.

Why this seam. The node routes run:

    repo.get_kube_client_config(cluster)   — reads a kubeconfig from disk
    KubeClientFactory().get_core_v1(cfg)   — builds a live CoreV1Api
    asyncio.to_thread(svc.<op>, kube=...)  — NodeService does the real work

Dry-run replaces the first two and leaves the third completely alone, so every
piece of business logic still executes for real: the uncordon readiness gate,
drain's refuse-before-evict check, the always-skipped pod categories, and the
per-node vs cluster-level failure layering in the batch endpoints. Those are
precisely what an e2e suite most needs to cover, and a seam any higher — short
-circuiting the router — would skip all of them while still answering 200.

This follows the convention the repo already uses in its tests: inject a fake
``CoreV1Api``-shaped object rather than patching ``kubernetes.client``. The
objects handed back are the SDK's own model classes (``V1Node``, ``V1Pod``, …),
which are plain data holders, so NodeService sees genuinely correctly-shaped
values instead of mocks that merely tolerate attribute access.

**State is mutable and shared per cluster.** Two separate requirements pull here
and both have to be met.

Within one request, an evicted pod must actually disappear from the next
``list_pod_for_all_namespaces``: ``_wait_for_pods_gone`` polls that call until
the targeted pods are gone or the 25s budget expires, so a fake with a fixed pod
list would turn every successful drain into a full-budget wait reporting
``still_terminating`` — a slow false failure rather than an obvious error.

Across requests, a cordon must still be in effect on the following read, because
that is how a real cluster behaves and an e2e test will reasonably assert it.
The factory builds a fresh client per request (preserving the real factory's
isolation guarantee, which exists to stop cross-cluster contamination under
concurrency), so the *client* is per-request while the *cluster state* it views
is shared and keyed by cluster name. That mirrors the real split: many clients,
one cluster. ``reset_dry_run_clusters()`` clears it between tests.

Design shared with deploy-service; see that repo's docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from kubernetes.client import (
    V1Node,
    V1NodeCondition,
    V1NodeList,
    V1NodeSpec,
    V1NodeStatus,
    V1NodeSystemInfo,
    V1ObjectMeta,
    V1OwnerReference,
    V1Pod,
    V1PodList,
    V1PodSpec,
    V1PodStatus,
    V1Taint,
    V1Volume,
)

_logger = logging.getLogger(__name__)

# Deliberately implausible so a value that leaks into a real system fails loudly
# rather than colliding with a genuine resource.
_KUBELET_VERSION = "v9.99.0-dry-run"

# Three nodes, chosen to exercise the readiness gate rather than just the happy
# path: a Ready node, a NotReady one, and one whose kubelet has gone silent.
# "Unknown" is a distinct third state (see NodeService._node_status) and is the
# case a denylist-style check would wrongly admit.
_READY_NODE = "dry-run-node-ready"
_NOT_READY_NODE = "dry-run-node-notready"
_UNKNOWN_NODE = "dry-run-node-unknown"

_NODE_READINESS = {
    _READY_NODE: "True",
    _NOT_READY_NODE: "False",
    _UNKNOWN_NODE: "Unknown",
}


def _system_info() -> V1NodeSystemInfo:
    """V1NodeSystemInfo requires every field, so build it in one place."""
    return V1NodeSystemInfo(
        architecture="amd64",
        boot_id="dry-run-boot-id",
        container_runtime_version="containerd://1.7.0-dry-run",
        kernel_version="0.0.0-dry-run",
        kube_proxy_version=_KUBELET_VERSION,
        kubelet_version=_KUBELET_VERSION,
        machine_id="dry-run-machine-id",
        operating_system="linux",
        os_image="Dry Run OS",
        system_uuid="dry-run-system-uuid",
    )


def _make_node(name: str, ready: str, unschedulable: bool = False) -> V1Node:
    return V1Node(
        metadata=V1ObjectMeta(
            name=name,
            labels={"kubernetes.io/hostname": name, "dry-run": "true"},
            annotations={},
        ),
        spec=V1NodeSpec(unschedulable=unschedulable, taints=[]),
        status=V1NodeStatus(
            conditions=[V1NodeCondition(type="Ready", status=ready)],
            node_info=_system_info(),
        ),
    )


def _make_pod(
    name: str,
    namespace: str = "default",
    node_name: str = _READY_NODE,
    owner_kind: Optional[str] = "ReplicaSet",
    phase: str = "Running",
    mirror: bool = False,
    empty_dir: bool = False,
) -> V1Pod:
    """Build one pod. The defaults describe an ordinary evictable pod; each
    keyword turns on exactly one of the categories drain treats specially."""
    annotations = {}
    if mirror:
        # NodeService keys mirror-pod detection off this annotation.
        annotations["kubernetes.io/config.mirror"] = "dry-run"

    owners = (
        [V1OwnerReference(api_version="apps/v1", kind=owner_kind, name=f"{name}-owner", uid="dry-run-uid")]
        if owner_kind
        else None
    )

    volumes = [V1Volume(name="scratch", empty_dir={})] if empty_dir else []

    return V1Pod(
        metadata=V1ObjectMeta(
            name=name,
            namespace=namespace,
            owner_references=owners,
            annotations=annotations,
        ),
        spec=V1PodSpec(node_name=node_name, containers=[], volumes=volumes),
        status=V1PodStatus(phase=phase, container_statuses=[]),
    )


def _default_pods() -> list[V1Pod]:
    """The pod set every dry-run cluster starts with.

    Covers each branch of drain's classification so the e2e suite exercises
    real decisions: two ordinary evictable pods, the three always-skipped
    categories, and the two *blocked* categories that make drain refuse unless
    the caller opts in. Without the blocked pair, refuse-before-evict could
    never fire and the most important guard in drain would go untested.
    """
    return [
        # Ordinary, evictable.
        _make_pod("dry-run-web-1"),
        _make_pod("dry-run-web-2", namespace="apps"),
        # Always skipped, never blocked, not user-configurable.
        _make_pod("dry-run-daemon", owner_kind="DaemonSet"),
        _make_pod("dry-run-mirror", owner_kind=None, mirror=True),
        _make_pod("dry-run-completed", phase="Succeeded"),
        # Blocked unless force / delete_emptydir_data is given.
        _make_pod("dry-run-unmanaged", owner_kind=None),
        _make_pod("dry-run-emptydir", empty_dir=True),
        # On another node — must not be touched by a drain of the ready node.
        _make_pod("dry-run-other-node", node_name=_NOT_READY_NODE),
    ]


class _ClusterState:
    """The mutable contents of one dry-run cluster."""

    def __init__(self) -> None:
        self.nodes: dict[str, V1Node] = {
            name: _make_node(name, ready) for name, ready in _NODE_READINESS.items()
        }
        self.pods: list[V1Pod] = _default_pods()


# Shared across the per-request client instances, keyed by cluster name. A real
# cluster persists what you did to it, so a cordon has to still be in effect on
# the next request; see the module docstring.
_CLUSTER_STATE: dict[str, _ClusterState] = {}


def _state_for(cluster: str) -> _ClusterState:
    return _CLUSTER_STATE.setdefault(cluster, _ClusterState())


def reset_dry_run_clusters() -> None:
    """Drop all dry-run cluster state.

    Process-global state leaks between tests, so a test that mutates a cluster
    must clear it — exactly as it would call ``get_settings.cache_clear()``.
    """
    _CLUSTER_STATE.clear()


class DryRunCoreV1Api:
    """Stand-in for ``CoreV1Api``, backed by mutable in-memory state.

    The surface below is exactly what NodeService touches: ``list_node``,
    ``read_node``, ``patch_node``, ``list_pod_for_all_namespaces``,
    ``list_namespaced_pod``, ``delete_namespaced_pod`` and
    ``create_namespaced_pod_eviction``.
    """

    def __init__(self, cluster: str = "dry-run-cluster") -> None:
        self._cluster = cluster

    @property
    def _nodes(self) -> dict[str, V1Node]:
        return _state_for(self._cluster).nodes

    @property
    def _pods(self) -> list[V1Pod]:
        return _state_for(self._cluster).pods

    @_pods.setter
    def _pods(self, value: list[V1Pod]) -> None:
        _state_for(self._cluster).pods = value

    # ── nodes ─────────────────────────────────────────────────────────────────

    def list_node(self, **_kwargs: Any) -> V1NodeList:
        """Also backs the batch uncordon readiness gate, which resolves every
        node's status from this single call rather than one read per node."""
        _logger.warning(
            "DRY-RUN | op=kube.list_node | no cluster was contacted | nodes=%d",
            len(self._nodes),
        )
        return V1NodeList(items=list(self._nodes.values()))

    def read_node(self, name: str, **_kwargs: Any) -> V1Node:
        _logger.warning(
            "DRY-RUN | op=kube.read_node | node=%s | no cluster was contacted", name
        )
        node = self._nodes.get(name)
        if node is None:
            raise _not_found(f"nodes/{name}")
        return node

    def patch_node(self, name: str, body: dict, **_kwargs: Any) -> V1Node:
        """Apply the patch to in-memory state.

        Applying it rather than discarding it is what lets a cordon be visible
        to the following read, so the API's own response reflects the change a
        caller just made instead of a fixed snapshot.
        """
        _logger.warning(
            "DRY-RUN | op=kube.patch_node | node=%s | patch=%s | nothing was changed "
            "on any cluster",
            name,
            body,
        )
        node = self._nodes.get(name)
        if node is None:
            raise _not_found(f"nodes/{name}")

        spec = (body or {}).get("spec", {})
        if "unschedulable" in spec:
            node.spec.unschedulable = bool(spec["unschedulable"])
        if "taints" in spec:
            # The patch body carries plain dicts, but a real read returns
            # V1Taint objects and NodeService reads `.key` / `.value` /
            # `.effect` off them. Storing the dicts verbatim would hand back a
            # shape the service cannot consume.
            node.spec.taints = [
                t if isinstance(t, V1Taint) else V1Taint(**t)
                for t in (spec["taints"] or [])
            ]

        metadata = (body or {}).get("metadata", {})
        for field in ("labels", "annotations"):
            patch = metadata.get(field)
            if not patch:
                continue
            current = dict(getattr(node.metadata, field) or {})
            for key, value in patch.items():
                # A null value is Kubernetes' removal semantics for a merge
                # patch — dropping the key, not storing None.
                if value is None:
                    current.pop(key, None)
                else:
                    current[key] = value
            setattr(node.metadata, field, current)

        return node

    # ── pods ──────────────────────────────────────────────────────────────────

    def list_pod_for_all_namespaces(
        self, field_selector: Optional[str] = None, **_kwargs: Any
    ) -> V1PodList:
        """Honours ``spec.nodeName=`` — the only selector NodeService sends.

        Honouring it matters: drain lists pods for one node, and a fake that
        ignored the selector would return another node's pods and then try to
        evict them.
        """
        items = self._pods
        if field_selector:
            for clause in field_selector.split(","):
                key, _, value = clause.partition("=")
                if key.strip() == "spec.nodeName":
                    items = [p for p in items if p.spec.node_name == value.strip()]
        _logger.warning(
            "DRY-RUN | op=kube.list_pod_for_all_namespaces | selector=%s | "
            "no cluster was contacted | pods=%d",
            field_selector,
            len(items),
        )
        return V1PodList(items=list(items))

    def list_namespaced_pod(self, namespace: str, **_kwargs: Any) -> V1PodList:
        _logger.warning(
            "DRY-RUN | op=kube.list_namespaced_pod | ns=%s | no cluster was contacted",
            namespace,
        )
        return V1PodList(
            items=[p for p in self._pods if p.metadata.namespace == namespace]
        )

    def delete_namespaced_pod(
        self, name: str, namespace: str, **_kwargs: Any
    ) -> V1Pod:
        """Used by drain when ``disable_eviction=true`` bypasses PDBs."""
        _logger.warning(
            "DRY-RUN | op=kube.delete_namespaced_pod | ns=%s | pod=%s | "
            "no pod was deleted",
            namespace,
            name,
        )
        return self._remove_pod(namespace, name)

    def create_namespaced_pod_eviction(
        self, name: str, namespace: str, body: Any = None, **_kwargs: Any
    ) -> Any:
        """The normal drain path (PDB-respecting eviction).

        Removing the pod from state is load-bearing, not cosmetic:
        ``_wait_for_pods_gone`` polls ``list_pod_for_all_namespaces`` until the
        targeted pods are gone, so a fake that kept them would spend the whole
        25s budget and report ``still_terminating`` on what should be a clean
        drain.
        """
        _logger.warning(
            "DRY-RUN | op=kube.create_namespaced_pod_eviction | ns=%s | pod=%s | "
            "no pod was evicted",
            namespace,
            name,
        )
        self._remove_pod(namespace, name)
        return body

    # ── internals ─────────────────────────────────────────────────────────────

    def _remove_pod(self, namespace: str, name: str) -> V1Pod:
        for index, pod in enumerate(self._pods):
            if pod.metadata.namespace == namespace and pod.metadata.name == name:
                return self._pods.pop(index)
        raise _not_found(f"{namespace}/{name}")


def _not_found(what: str):
    """Build the SDK's own 404, so NodeService's ``except ApiException`` paths
    (which map 404 → NodeNotFoundException) run exactly as in production."""
    from kubernetes.client.exceptions import ApiException

    exc = ApiException(status=404, reason="Not Found")
    exc.body = f'{{"message": "dry-run: {what} not found"}}'
    return exc
