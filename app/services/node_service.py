"""
app/services/node_service.py

NodeService — implements Kubernetes node operations.

Operations:
  - list_nodes  : list all nodes with labels included in response
  - cordon      : mark node unschedulable
  - uncordon    : re-enable scheduling
  - drain       : cordon + evict/delete eligible pods → return pod list
  - label_node  : arbitrary set / remove of node labels
  - annotate_node: arbitrary set / remove of node annotations
  - taint_node  : set / remove node taints (recomputes spec.taints list)

Design decisions
──────────────────
- DaemonSet pods are ALWAYS skipped during drain — not user-configurable.
- ``dry_run`` is resolved at the API layer before the service is called.
- Drain returns ``DrainActionData`` (superset of NodeActionData) including
  the list of pods that were evicted or deleted.
"""

from __future__ import annotations

import logging
import time

from kubernetes.client import CoreV1Api, V1Node
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app.core.exceptions import KubeApiException, NodeNotFoundException
from app.domain.kubernetes_models import (
    DrainActionData,
    DrainOptions,
    DrainedPodInfo,
    NodeActionData,
    NodeAnnotationsData,
    NodeDetailData,
    NodeInfo,
    NodeLabelsData,
    NodeListData,
    NodeTaintData,
    PodInfo,
    PodListData,
    TaintRemoveSpec,
    TaintSpec,
)

_logger = logging.getLogger(__name__)

# Annotation that identifies mirror / static pods — not evictable.
_MIRROR_POD_ANNOTATION = "kubernetes.io/config.mirror"


def _connection_error(cluster: str, exc: Urllib3HTTPError) -> KubeApiException:
    """Convert a urllib3 network error to a KubeApiException(503)."""
    return KubeApiException(
        f"Cannot reach cluster '{cluster}': {exc}",
        kube_status=503,
    )


class NodeService:
    """Implements cordon / uncordon / drain / list / label / annotate operations."""

    # ── Node listing ──────────────────────────────────────────────────────────

    def list_nodes(self, cluster: str, kube: CoreV1Api) -> NodeListData:
        """Fetch all nodes, including their label map.

        Raises:
            KubeApiException: On Kubernetes API failure.
        """
        try:
            node_list = kube.list_node()
        except ApiException as exc:
            raise KubeApiException(
                f"Failed to list nodes in cluster '{cluster}': {exc.reason}",
                kube_status=exc.status,
            ) from exc
        except Urllib3HTTPError as exc:
            raise _connection_error(cluster, exc) from exc

        nodes = [self._node_to_info(n) for n in node_list.items]
        _logger.info("Listed %d node(s) | cluster=%s", len(nodes), cluster)
        return NodeListData(cluster=cluster, nodes=nodes)

    def list_pods(
        self,
        cluster: str,
        namespace: str,
        kube: CoreV1Api,
        nodes: list[str] | None = None,
        statuses: list[str] | None = None,
        name_prefixes: list[str] | None = None,
    ) -> PodListData:
        """List pods in *namespace*, filtered by node / status / name prefix.

        When ``namespace == "*"`` lists pods across all namespaces; otherwise
        scopes to the single namespace.

        Filter semantics: values within a parameter are OR'd; the three
        parameters are AND'd. An empty/None parameter does not filter that
        dimension. ``statuses`` matches pod phase, case-insensitive.
        ``name_prefixes`` is a prefix match.

        Raises:
            KubeApiException: On Kubernetes API failure.
        """
        try:
            if namespace == "*":
                pod_list = kube.list_pod_for_all_namespaces()
            else:
                pod_list = kube.list_namespaced_pod(namespace)
        except ApiException as exc:
            raise KubeApiException(
                f"Failed to list pods in namespace '{namespace}' "
                f"of cluster '{cluster}': {exc.reason}",
                kube_status=exc.status,
            ) from exc
        except Urllib3HTTPError as exc:
            raise _connection_error(cluster, exc) from exc

        node_set = set(nodes) if nodes else None
        status_set = {s.lower() for s in statuses} if statuses else None
        prefixes = tuple(name_prefixes) if name_prefixes else None

        pods: list[PodInfo] = []
        for raw in pod_list.items:
            info = self._pod_to_info(raw)
            if node_set is not None and info.node_name not in node_set:
                continue
            if status_set is not None and info.phase.lower() not in status_set:
                continue
            if prefixes is not None and not info.name.startswith(prefixes):
                continue
            pods.append(info)

        _logger.info(
            "Listed %d pod(s) | cluster=%s | namespace=%s",
            len(pods), cluster, namespace,
        )
        return PodListData(cluster=cluster, namespace=namespace, pods=pods)

    # ── Single node detail ────────────────────────────────────────────────────

    def get_node(self, cluster: str, node_name: str, kube: CoreV1Api) -> NodeDetailData:
        """Fetch full detail for a single node (node attributes only).

        Pods are queried via the dedicated pods endpoint, not here.

        Raises:
            NodeNotFoundException: If the node does not exist.
            KubeApiException: On Kubernetes API failure.
        """
        try:
            node = kube.read_node(node_name)
        except ApiException as exc:
            if exc.status == 404:
                raise NodeNotFoundException(
                    f"Node '{node_name}' not found in cluster '{cluster}'.",
                ) from exc
            raise KubeApiException(
                f"Failed to read node '{node_name}': {exc.reason}",
                kube_status=exc.status,
            ) from exc
        except Urllib3HTTPError as exc:
            raise _connection_error(cluster, exc) from exc

        info = self._node_to_info(node)
        taints = [self._to_taint_spec(t) for t in (node.spec.taints or [])]
        _logger.info("Got node detail | cluster=%s | node=%s", cluster, node_name)
        return NodeDetailData(
            cluster=cluster,
            name=info.name,
            status=info.status,
            roles=info.roles,
            version=info.version,
            unschedulable=info.unschedulable,
            labels=info.labels,
            annotations=info.annotations,
            taints=taints,
        )

    # ── Cordon ────────────────────────────────────────────────────────────────

    def cordon(self, cluster: str, node_name: str, kube: CoreV1Api) -> NodeActionData:
        """Mark *node_name* as unschedulable.

        Raises:
            NodeNotFoundException: If the node does not exist.
            KubeApiException: On Kubernetes API failure.
        """
        self._patch_unschedulable(cluster, node_name, kube, unschedulable=True)
        _logger.info("Cordoned node | cluster=%s | node=%s", cluster, node_name)
        return NodeActionData(cluster=cluster, node=node_name, action="cordon")

    # ── Uncordon ──────────────────────────────────────────────────────────────

    def uncordon(self, cluster: str, node_name: str, kube: CoreV1Api) -> NodeActionData:
        """Re-enable scheduling.

        Raises:
            NodeNotFoundException: If the node does not exist.
            KubeApiException: On Kubernetes API failure.
        """
        self._patch_unschedulable(cluster, node_name, kube, unschedulable=False)
        _logger.info("Uncordoned node | cluster=%s | node=%s", cluster, node_name)
        return NodeActionData(cluster=cluster, node=node_name, action="uncordon")

    # ── Drain ─────────────────────────────────────────────────────────────────

    def drain(
        self,
        cluster: str,
        node_name: str,
        kube: CoreV1Api,
        options: DrainOptions,
    ) -> DrainActionData:
        """Drain *node_name*: cordon → evict/delete eligible pods → return pod list.

        DaemonSet pods are ALWAYS skipped (not configurable).
        Mirror/static pods are ALWAYS skipped.
        Completed/failed pods are ALWAYS skipped.

        Steps:
          1. Cordon the node (unschedulable=True).
          2. List all pods assigned to the node.
          3. Filter out ineligible pods.
          4. Evict (honour PDB) or delete (bypass PDB) each eligible pod.
          5. Poll until all targeted pods are gone or timeout expires.

        Returns:
            DrainActionData including the list of pods that were processed.

        Raises:
            NodeNotFoundException: Node does not exist.
            KubeApiException: On Kubernetes API failure.
        """
        # Step 1 — cordon first.
        self.cordon(cluster, node_name, kube)
        _logger.info(
            "Draining node | cluster=%s | node=%s | options=%s",
            cluster, node_name, options.model_dump(),
        )

        # Step 2 — collect pods assigned to this node.
        try:
            pod_list = kube.list_pod_for_all_namespaces(
                field_selector=f"spec.nodeName={node_name}"
            )
        except ApiException as exc:
            raise KubeApiException(
                f"Failed to list pods on node '{node_name}': {exc.reason}",
                kube_status=exc.status,
            ) from exc
        except Urllib3HTTPError as exc:
            raise _connection_error(cluster, exc) from exc

        # Step 3 — filter ineligible pods.
        pods_to_evict = []
        for pod in pod_list.items:
            annotations = pod.metadata.annotations or {}
            owner_kinds = [ref.kind for ref in (pod.metadata.owner_references or [])]

            if _MIRROR_POD_ANNOTATION in annotations:
                _logger.debug("Skipping mirror pod | pod=%s", pod.metadata.name)
                continue

            phase = (pod.status.phase or "").lower()
            if phase in ("succeeded", "failed"):
                _logger.debug(
                    "Skipping completed pod | pod=%s | phase=%s",
                    pod.metadata.name, phase,
                )
                continue

            # DaemonSet pods are ALWAYS skipped — API enforces this.
            if "DaemonSet" in owner_kinds:
                _logger.debug("Skipping DaemonSet pod | pod=%s", pod.metadata.name)
                continue

            pods_to_evict.append(pod)

        _logger.info(
            "Pods to evict | cluster=%s | node=%s | count=%d",
            cluster, node_name, len(pods_to_evict),
        )

        # Step 4 — evict or delete each pod.
        for pod in pods_to_evict:
            self._evict_or_delete(
                kube=kube,
                name=pod.metadata.name,
                namespace=pod.metadata.namespace,
                options=options,
            )

        # Step 5 — wait for pods to terminate.
        self._wait_for_pods_gone(
            kube=kube,
            node_name=node_name,
            pod_names={(p.metadata.namespace, p.metadata.name) for p in pods_to_evict},
            timeout_seconds=options.timeout_seconds,
        )

        drained_pods = [
            DrainedPodInfo(name=p.metadata.name, namespace=p.metadata.namespace)
            for p in pods_to_evict
        ]
        _logger.info("Drain complete | cluster=%s | node=%s | drained=%d", cluster, node_name, len(drained_pods))
        return DrainActionData(
            cluster=cluster,
            node=node_name,
            action="drain",
            drained_pods=drained_pods,
        )

    # ── Label management ──────────────────────────────────────────────────────

    def label_node(
        self,
        cluster: str,
        node_name: str,
        kube: CoreV1Api,
        set_labels: dict[str, str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> NodeLabelsData:
        """Add / overwrite or remove labels on *node_name*.

        Returns the node's current labels after the patch.

        Raises:
            NodeNotFoundException: Node does not exist.
            KubeApiException: On Kubernetes API failure.
        """
        patched = self._patch_labels(
            cluster, node_name, kube,
            set_labels=set_labels, remove_labels=remove_labels,
        )
        current_labels = self._fetch_node_labels(cluster, node_name, kube) if patched else {}
        _logger.info("Patched labels | cluster=%s | node=%s", cluster, node_name)
        return NodeLabelsData(cluster=cluster, node=node_name, labels=current_labels)

    # ── Annotation management ─────────────────────────────────────────────────

    def annotate_node(
        self,
        cluster: str,
        node_name: str,
        kube: CoreV1Api,
        set_annotations: dict[str, str] | None = None,
        remove_annotations: list[str] | None = None,
    ) -> NodeAnnotationsData:
        """Add / overwrite or remove annotations on *node_name*.

        Returns the node's current annotations after the patch.

        Raises:
            NodeNotFoundException: Node does not exist.
            KubeApiException: On Kubernetes API failure.
        """
        patched = self._patch_annotations(
            cluster, node_name, kube,
            set_annotations=set_annotations, remove_annotations=remove_annotations,
        )
        current_annotations = self._fetch_node_annotations(cluster, node_name, kube) if patched else {}
        _logger.info("Patched annotations | cluster=%s | node=%s", cluster, node_name)
        return NodeAnnotationsData(cluster=cluster, node=node_name, annotations=current_annotations)

    # ── Taint management ──────────────────────────────────────────────────────

    def taint_node(
        self,
        cluster: str,
        node_name: str,
        kube: CoreV1Api,
        set_taints: list[TaintSpec] | None = None,
        remove_taints: list[TaintRemoveSpec] | None = None,
    ) -> NodeTaintData:
        """Set / remove taints on *node_name*; return current taints.

        ``spec.taints`` is a list, not a map, so we read the current taints,
        recompute the full list (remove first, then set — keyed by
        ``(key, effect)`` so a set overwrites an existing taint's value), and
        patch the whole list. Empty set+remove skips the patch.

        Raises:
            NodeNotFoundException: Node does not exist.
            KubeApiException: On Kubernetes API failure.
        """
        set_taints = set_taints or []
        remove_taints = remove_taints or []

        current = self._read_node(cluster, node_name, kube)
        existing = current.spec.taints or []

        by_id: dict[tuple[str, str], dict] = {}
        for t in existing:
            by_id[(t.key, t.effect)] = {"key": t.key, "value": t.value, "effect": t.effect}

        if set_taints or remove_taints:
            for r in remove_taints:
                by_id.pop((r.key, r.effect), None)
            for s in set_taints:
                by_id[(s.key, s.effect)] = {"key": s.key, "value": s.value, "effect": s.effect}

            new_taints = list(by_id.values())
            try:
                kube.patch_node(node_name, {"spec": {"taints": new_taints}})
            except ApiException as exc:
                if exc.status == 404:
                    raise NodeNotFoundException(
                        f"Node '{node_name}' not found in cluster '{cluster}'.",
                    ) from exc
                raise KubeApiException(
                    f"Failed to patch taints on node '{node_name}': {exc.reason}",
                    kube_status=exc.status,
                ) from exc
            except Urllib3HTTPError as exc:
                raise _connection_error(cluster, exc) from exc
            current = self._read_node(cluster, node_name, kube)
            _logger.info("Patched taints | cluster=%s | node=%s", cluster, node_name)

        taints = [self._to_taint_spec(t) for t in (current.spec.taints or [])]
        return NodeTaintData(cluster=cluster, node=node_name, taints=taints)

    # ── Private helpers ───────────────────────────────────────────────────────

    def _patch_unschedulable(
        self,
        cluster: str,
        node_name: str,
        kube: CoreV1Api,
        *,
        unschedulable: bool,
    ) -> None:
        """Patch spec.unschedulable on the node."""
        try:
            kube.patch_node(node_name, {"spec": {"unschedulable": unschedulable}})
        except ApiException as exc:
            if exc.status == 404:
                raise NodeNotFoundException(
                    f"Node '{node_name}' not found in cluster '{cluster}'.",
                ) from exc
            raise KubeApiException(
                f"Failed to patch node '{node_name}': {exc.reason}",
                kube_status=exc.status,
            ) from exc
        except Urllib3HTTPError as exc:
            raise _connection_error(cluster, exc) from exc

    def _patch_labels(
        self,
        cluster: str,
        node_name: str,
        kube: CoreV1Api,
        set_labels: dict[str, str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> bool:
        """Apply label additions and deletions in a single patch call.

        Returns True if a patch was actually sent, False when nothing to do.
        """
        labels: dict[str, str | None] = {}
        labels.update(set_labels or {})
        for key in remove_labels or []:
            labels[key] = None  # null value → Kubernetes deletes the label

        if not labels:
            return False

        try:
            kube.patch_node(node_name, {"metadata": {"labels": labels}})
        except ApiException as exc:
            if exc.status == 404:
                raise NodeNotFoundException(
                    f"Node '{node_name}' not found in cluster '{cluster}'.",
                ) from exc
            raise KubeApiException(
                f"Failed to patch labels on node '{node_name}': {exc.reason}",
                kube_status=exc.status,
            ) from exc
        except Urllib3HTTPError as exc:
            raise _connection_error(cluster, exc) from exc
        return True

    def _patch_annotations(
        self,
        cluster: str,
        node_name: str,
        kube: CoreV1Api,
        set_annotations: dict[str, str] | None = None,
        remove_annotations: list[str] | None = None,
    ) -> bool:
        """Apply annotation additions and deletions in a single patch call.

        Returns True if a patch was actually sent, False when nothing to do.
        """
        annotations: dict[str, str | None] = {}
        annotations.update(set_annotations or {})
        for key in remove_annotations or []:
            annotations[key] = None

        if not annotations:
            return False

        try:
            kube.patch_node(node_name, {"metadata": {"annotations": annotations}})
        except ApiException as exc:
            if exc.status == 404:
                raise NodeNotFoundException(
                    f"Node '{node_name}' not found in cluster '{cluster}'.",
                ) from exc
            raise KubeApiException(
                f"Failed to patch annotations on node '{node_name}': {exc.reason}",
                kube_status=exc.status,
            ) from exc
        except Urllib3HTTPError as exc:
            raise _connection_error(cluster, exc) from exc
        return True

    def _fetch_node_labels(self, cluster: str, node_name: str, kube: CoreV1Api) -> dict[str, str]:
        """Read current labels from the cluster after a patch."""
        node = self._read_node(cluster, node_name, kube)
        return node.metadata.labels or {}

    def _fetch_node_annotations(self, cluster: str, node_name: str, kube: CoreV1Api) -> dict[str, str]:
        """Read current annotations from the cluster after a patch."""
        node = self._read_node(cluster, node_name, kube)
        return node.metadata.annotations or {}

    def _read_node(self, cluster: str, node_name: str, kube: CoreV1Api):
        """Read a node, mapping 404 → NodeNotFoundException."""
        try:
            return kube.read_node(node_name)
        except ApiException as exc:
            if exc.status == 404:
                raise NodeNotFoundException(
                    f"Node '{node_name}' not found in cluster '{cluster}'.",
                ) from exc
            raise KubeApiException(
                f"Failed to read node '{node_name}': {exc.reason}",
                kube_status=exc.status,
            ) from exc
        except Urllib3HTTPError as exc:
            raise _connection_error(cluster, exc) from exc

    @staticmethod
    def _to_taint_spec(taint) -> TaintSpec:
        """Convert a V1Taint (or fake) to a TaintSpec."""
        return TaintSpec(key=taint.key, value=taint.value, effect=taint.effect)

    def _evict_or_delete(
        self,
        kube: CoreV1Api,
        name: str,
        namespace: str,
        options: DrainOptions,
    ) -> None:
        """Evict (honour PDB) or delete (bypass PDB) a single pod."""
        grace = options.grace_period_seconds

        if options.disable_eviction:
            _logger.debug("Deleting pod | ns=%s | pod=%s", namespace, name)
            try:
                kube.delete_namespaced_pod(
                    name=name, namespace=namespace, grace_period_seconds=grace,
                )
            except ApiException as exc:
                if exc.status == 404:
                    return  # already gone
                raise KubeApiException(
                    f"Failed to delete pod '{namespace}/{name}': {exc.reason}",
                    kube_status=exc.status,
                ) from exc
            except Urllib3HTTPError as exc:
                raise KubeApiException(
                    f"Cannot reach cluster: {exc}", kube_status=503,
                ) from exc
        else:
            from kubernetes.client.models import V1DeleteOptions, V1Eviction, V1ObjectMeta
            _logger.debug("Evicting pod | ns=%s | pod=%s", namespace, name)
            eviction = V1Eviction(
                metadata=V1ObjectMeta(name=name, namespace=namespace),
                delete_options=V1DeleteOptions(grace_period_seconds=grace),
            )
            try:
                kube.create_namespaced_pod_eviction(name=name, namespace=namespace, body=eviction)
            except ApiException as exc:
                if exc.status == 404:
                    return
                if exc.status == 429:
                    raise KubeApiException(
                        f"Pod '{namespace}/{name}' cannot be evicted due to a "
                        "PodDisruptionBudget. Use disable_eviction=true to bypass.",
                        kube_status=409,
                    ) from exc
                raise KubeApiException(
                    f"Failed to evict pod '{namespace}/{name}': {exc.reason}",
                    kube_status=exc.status,
                ) from exc
            except Urllib3HTTPError as exc:
                raise KubeApiException(
                    f"Cannot reach cluster: {exc}", kube_status=503,
                ) from exc

    def _wait_for_pods_gone(
        self,
        kube: CoreV1Api,
        node_name: str,
        pod_names: set[tuple[str, str]],
        timeout_seconds: int,
    ) -> None:
        if not pod_names:
            return
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                remaining = kube.list_pod_for_all_namespaces(
                    field_selector=f"spec.nodeName={node_name}"
                )
            except ApiException as exc:
                raise KubeApiException(
                    f"Error while waiting for pods to drain: {exc.reason}",
                    kube_status=exc.status,
                ) from exc
            except Urllib3HTTPError as exc:
                raise KubeApiException(
                    f"Cannot reach cluster: {exc}", kube_status=503,
                ) from exc

            still_present = {
                (p.metadata.namespace, p.metadata.name)
                for p in remaining.items
                if (p.metadata.namespace, p.metadata.name) in pod_names
            }
            if not still_present:
                _logger.debug("All targeted pods are gone | node=%s", node_name)
                return
            _logger.debug(
                "Waiting for %d pod(s) to terminate | node=%s",
                len(still_present), node_name,
            )
            time.sleep(2)

        raise KubeApiException(
            f"Drain timed out after {timeout_seconds}s: some pods are still running on '{node_name}'.",
            kube_status=504,
        )

    @staticmethod
    def _node_to_info(node: V1Node) -> NodeInfo:
        """Convert a V1Node object to a NodeInfo response model."""
        status = "Unknown"
        for cond in (node.status.conditions or []):
            if cond.type == "Ready":
                status = "Ready" if cond.status == "True" else "NotReady"
                break

        labels = node.metadata.labels or {}
        roles = [
            key.split("/")[-1]
            for key in labels
            if key.startswith("node-role.kubernetes.io/")
        ] or ["<none>"]

        version = (
            node.status.node_info.kubelet_version if node.status.node_info else ""
        )

        return NodeInfo(
            name=node.metadata.name,
            status=status,
            roles=roles,
            version=version,
            unschedulable=bool(node.spec.unschedulable),
            labels=labels,
            annotations=node.metadata.annotations or {},
        )

    @staticmethod
    def _pod_to_info(pod) -> PodInfo:
        """Convert a V1Pod object to a PodInfo summary."""
        owner_kind: str | None = None
        if pod.metadata.owner_references:
            owner_kind = pod.metadata.owner_references[0].kind

        # Sum restarts across all container statuses.
        restart_count = 0
        ready = False
        container_statuses = pod.status.container_statuses or []
        if container_statuses:
            restart_count = sum(cs.restart_count or 0 for cs in container_statuses)
            ready = all(cs.ready for cs in container_statuses)

        return PodInfo(
            name=pod.metadata.name,
            namespace=pod.metadata.namespace,
            phase=pod.status.phase or "Unknown",
            ready=ready,
            owner_kind=owner_kind,
            restart_count=restart_count,
            node_name=(pod.spec.node_name or "") if pod.spec else "",
        )
