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
from collections.abc import Callable

from kubernetes.client import CoreV1Api, V1Node
from kubernetes.client.exceptions import ApiException

from app.core.config import get_settings
from app.core.exceptions import (
    DrainBlockedException,
    KubeApiException,
    NodeNotFoundException,
    NodeNotReadyException,
)
from app.domain.kubernetes_models import (
    BatchNodeActionData,
    BatchNodeResult,
    BatchSummary,
    DrainActionData,
    DrainOptions,
    DrainedPodInfo,
    StillTerminatingPodInfo,
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
from app.services.kube_errors import translate_kube_errors

_logger = logging.getLogger(__name__)

# Annotation that identifies mirror / static pods — not evictable.
_MIRROR_POD_ANNOTATION = "kubernetes.io/config.mirror"


def _node_not_found(
    cluster: str, node_name: str
) -> Callable[[], NodeNotFoundException]:
    """Builds the node's not-found exception on demand — the ``not_found``
    factory ``translate_kube_errors`` calls on a 404."""
    return lambda: NodeNotFoundException(
        f"Node '{node_name}' not found in cluster '{cluster}'.",
    )


class NodeService:
    """Implements cordon / uncordon / drain / list / label / annotate operations."""

    # ── Node listing ──────────────────────────────────────────────────────────

    def list_nodes(self, cluster: str, kube: CoreV1Api) -> NodeListData:
        """Fetch all nodes, including their label map.

        Raises:
            KubeApiException: On Kubernetes API failure.
        """
        with translate_kube_errors(cluster, f"list nodes in cluster '{cluster}'"):
            node_list = kube.list_node()

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
        with translate_kube_errors(
            cluster, f"list pods in namespace '{namespace}' of cluster '{cluster}'"
        ):
            if namespace == "*":
                pod_list = kube.list_pod_for_all_namespaces()
            else:
                pod_list = kube.list_namespaced_pod(namespace)

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
        node = self._read_node(cluster, node_name, kube)

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
        """Re-enable scheduling, but only on a node that is currently Ready.

        The readiness gate has no override. Uncordoning states that the node is
        fit for work, and there is no request the caller can send that makes an
        unhealthy node fit — so the fix is always to the node, never to the call.

        This guards against acting on a stale view of the cluster; it does not
        make scheduling safe. See NodeNotReadyException for why a flapping node
        still gets filled.

        Raises:
            NodeNotFoundException: If the node does not exist.
            NodeNotReadyException: If the node is not Ready (409).
            KubeApiException: On Kubernetes API failure.
        """
        node = self._read_node(cluster, node_name, kube)
        status = self._node_status(node)
        if status != "Ready":
            _logger.warning(
                "Refused uncordon of non-Ready node | cluster=%s | node=%s | status=%s",
                cluster, node_name, status,
            )
            raise NodeNotReadyException(node_name=node_name, status=status)

        self._patch_unschedulable(cluster, node_name, kube, unschedulable=False)
        _logger.info("Uncordoned node | cluster=%s | node=%s", cluster, node_name)
        return NodeActionData(cluster=cluster, node=node_name, action="uncordon")

    # ── Batch cordon / uncordon ───────────────────────────────────────────────

    def cordon_many(
        self,
        cluster: str,
        node_names: list[str],
        kube: CoreV1Api,
    ) -> BatchNodeActionData:
        """Cordon several nodes, reporting a result for each.

        Node-level failures are collected into the response; they never abort
        the batch. Cluster-level failures (unreachable API server) propagate.
        """
        return self._batch_set_unschedulable(
            cluster, node_names, kube, unschedulable=True, action="cordon",
        )

    def uncordon_many(
        self,
        cluster: str,
        node_names: list[str],
        kube: CoreV1Api,
    ) -> BatchNodeActionData:
        """Uncordon several nodes, reporting a result for each.

        Node-level failures are collected into the response; they never abort
        the batch. Cluster-level failures (unreachable API server) propagate.
        """
        return self._batch_set_unschedulable(
            cluster, node_names, kube, unschedulable=False, action="uncordon",
        )

    @staticmethod
    def _is_cluster_level(exc: Exception) -> bool:
        """True when a failure is about the cluster, not the individual node.

        Reporting these per node would tell the caller "these N nodes are bad"
        when the real answer is "the cluster is unreachable" or "your
        credentials are dead" — so a batch propagates them instead.
        """
        if getattr(exc, "cluster_level", False):
            return True
        if not isinstance(exc, KubeApiException):
            # NodeNotFound / NodeNotReady are about one node by construction.
            return False
        # 401/403 come from the API server, so they arrive untagged — but they
        # are a property of the connection, and will repeat for every node.
        # No status at all means no HTTP response (TLS failure, a request the
        # SDK could not build): nothing about any one node was learned, and it
        # repeats identically for every node, like a connection error.
        return exc.kube_status in (401, 403) or exc.kube_status is None

    def _batch_set_unschedulable(
        self,
        cluster: str,
        node_names: list[str],
        kube: CoreV1Api,
        *,
        unschedulable: bool,
        action: str,
    ) -> BatchNodeActionData:
        """Shared batch loop for cordon_many / uncordon_many.

        Nodes are de-duplicated (first-seen order kept) so ``results`` is safe
        to key by node name. Execution is sequential: Kubernetes offers no
        transaction across N node patches, so concurrency would only add
        API-server load to an already-cheap operation.

        Only per-node failures are captured into ``results``. Cluster-level
        failures — an unreachable API server, bad credentials — propagate, so
        callers can tell "the cluster is down" from "these nodes are bad"
        instead of reading N identical per-node errors. Any unexpected
        exception type propagates too, rather than being silently downgraded.

        Uncordon additionally requires each node to be Ready. Readiness for the
        whole batch comes from one ``list_node`` rather than a read per node:
        100 nodes would otherwise cost 100 extra round-trips to answer a
        question one listing already answers. A node that is not Ready — or is
        missing from the listing entirely — fails only itself, even when every
        node in the batch fails that way. "All of them failed" looks like a
        cluster problem but is not evidence of one, and `_is_cluster_level`
        stays keyed on signals that are certain (connection errors, 401/403)
        rather than on a guess.
        """
        results: list[BatchNodeResult] = []
        # Only uncordon gates on readiness, and only when there is work to do:
        # `_strip_node_names` drops blank entries after min_length validation,
        # so an all-blank body arrives here empty and must not buy a listing
        # for a loop that will not run.
        statuses = (
            self._node_statuses(cluster, kube)
            if not unschedulable and node_names
            else {}
        )

        for node_name in dict.fromkeys(node_names):
            try:
                if not unschedulable:
                    # Missing from the listing means the node is gone; let the
                    # patch produce the usual 404 rather than inventing one.
                    status = statuses.get(node_name)
                    if status is not None and status != "Ready":
                        raise NodeNotReadyException(
                            node_name=node_name, status=status,
                        )
                self._patch_unschedulable(
                    cluster, node_name, kube, unschedulable=unschedulable,
                )
                results.append(BatchNodeResult(node=node_name, status="success"))
            except (
                NodeNotFoundException,
                NodeNotReadyException,
                KubeApiException,
            ) as exc:
                if self._is_cluster_level(exc):
                    raise
                # NodeNotFoundException / NodeNotReadyException carry no
                # kube_status of their own (each is raised on one known status),
                # so they fall back to the app-level status. A KubeApiException
                # reports exactly what the API server said — None included,
                # rather than a fabricated 502.
                results.append(
                    BatchNodeResult(
                        node=node_name,
                        status="failed",
                        error_code=str(exc.error_code),
                        message=str(exc),
                        kube_status=(
                            exc.kube_status
                            if isinstance(exc, KubeApiException)
                            else exc.http_status
                        ),
                    )
                )

        succeeded = sum(1 for r in results if r.status == "success")
        return BatchNodeActionData(
            cluster=cluster,
            action=action,
            summary=BatchSummary(
                total=len(results),
                succeeded=succeeded,
                failed=len(results) - succeeded,
            ),
            results=results,
        )

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
        with translate_kube_errors(cluster, f"list pods on node '{node_name}'"):
            pod_list = kube.list_pod_for_all_namespaces(
                field_selector=f"spec.nodeName={node_name}"
            )

        # Step 3 — classify. Skips are unconditional; blocks are opt-out.
        pods_to_evict = []
        blocked: list[dict] = []
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

            # Protected categories. Each is removable only with its opt-out
            # flag; without it the whole drain is refused before anything is
            # evicted. Both reasons are collected, never short-circuited, so
            # one round-trip reports every flag the caller needs.
            reasons: list[str] = []
            if not owner_kinds and not options.force:
                reasons.append("unmanaged")
            if self._uses_emptydir(pod) and not options.delete_emptydir_data:
                reasons.append("emptydir")

            if reasons:
                blocked.append({
                    "namespace": pod.metadata.namespace,
                    "name": pod.metadata.name,
                    "reasons": reasons,
                })
                continue

            pods_to_evict.append(pod)

        # Refuse before evicting anything. A half-drained node is worse than an
        # untouched one: the pods already killed cannot be brought back, and the
        # node is still not empty.
        if blocked:
            _logger.warning(
                "Drain blocked | cluster=%s | node=%s | blocked=%d",
                cluster, node_name, len(blocked),
            )
            raise DrainBlockedException(node_name=node_name, blocked=blocked)

        _logger.info(
            "Pods to evict | cluster=%s | node=%s | count=%d",
            cluster, node_name, len(pods_to_evict),
        )

        # Step 4 — evict or delete each pod.
        for pod in pods_to_evict:
            self._evict_or_delete(
                cluster=cluster,
                kube=kube,
                name=pod.metadata.name,
                namespace=pod.metadata.namespace,
                options=options,
            )

        # Step 5 — watch for termination. Exceeding the budget is NOT a failure:
        # every eviction was accepted, and a pod with a long grace period is
        # shutting down exactly as configured. The leftovers are reported in the
        # 200 response so the caller can see what is still going without
        # diffing two pod listings.
        timeout_seconds = get_settings().DRAIN_DEFAULT_TIMEOUT_SECONDS
        still_present = self._wait_for_pods_gone(
            cluster=cluster,
            kube=kube,
            node_name=node_name,
            pod_names={(p.metadata.namespace, p.metadata.name) for p in pods_to_evict},
            timeout_seconds=timeout_seconds,
        )

        drained_pods = [
            DrainedPodInfo(name=p.metadata.name, namespace=p.metadata.namespace)
            for p in pods_to_evict
        ]
        still_terminating = [
            StillTerminatingPodInfo(name=name, namespace=ns)
            for ns, name in sorted(still_present)
        ]
        _logger.info(
            "Drain complete | cluster=%s | node=%s | drained=%d | still_terminating=%d",
            cluster, node_name, len(drained_pods), len(still_terminating),
        )
        return DrainActionData(
            cluster=cluster,
            node=node_name,
            action="drain",
            drained_pods=drained_pods,
            still_terminating=still_terminating,
            node_emptied=not still_terminating,
            forced_deletion=options.grace_period_seconds == 0,
        )

    @staticmethod
    def _uses_emptydir(pod) -> bool:
        """True when the pod mounts at least one emptyDir volume.

        emptyDir lives and dies with the pod, so evicting one destroys data
        that exists nowhere else — hence the opt-out flag.
        """
        volumes = (pod.spec.volumes or []) if pod.spec else []
        return any(v.empty_dir is not None for v in volumes)

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
            with translate_kube_errors(
                cluster,
                f"patch taints on node '{node_name}'",
                not_found=_node_not_found(cluster, node_name),
            ):
                kube.patch_node(node_name, {"spec": {"taints": new_taints}})
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
        with translate_kube_errors(
            cluster,
            f"patch node '{node_name}'",
            not_found=_node_not_found(cluster, node_name),
        ):
            kube.patch_node(node_name, {"spec": {"unschedulable": unschedulable}})

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

        with translate_kube_errors(
            cluster,
            f"patch labels on node '{node_name}'",
            not_found=_node_not_found(cluster, node_name),
        ):
            kube.patch_node(node_name, {"metadata": {"labels": labels}})
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

        with translate_kube_errors(
            cluster,
            f"patch annotations on node '{node_name}'",
            not_found=_node_not_found(cluster, node_name),
        ):
            kube.patch_node(node_name, {"metadata": {"annotations": annotations}})
        return True

    def _fetch_node_labels(self, cluster: str, node_name: str, kube: CoreV1Api) -> dict[str, str]:
        """Read current labels from the cluster after a patch."""
        node = self._read_node(cluster, node_name, kube)
        return node.metadata.labels or {}

    def _fetch_node_annotations(self, cluster: str, node_name: str, kube: CoreV1Api) -> dict[str, str]:
        """Read current annotations from the cluster after a patch."""
        node = self._read_node(cluster, node_name, kube)
        return node.metadata.annotations or {}

    def _node_statuses(self, cluster: str, kube: CoreV1Api) -> dict[str, str]:
        """Map every node name in the cluster to its readiness, in one call.

        Used by the batch uncordon gate so N nodes cost one listing rather than
        N reads. A failure here is cluster-level by construction — the listing
        is not about any single node — so it propagates rather than being
        recorded against whichever node happened to be first.
        """
        with translate_kube_errors(cluster, f"list nodes in cluster '{cluster}'"):
            node_list = kube.list_node()

        return {n.metadata.name: self._node_status(n) for n in node_list.items}

    def _read_node(self, cluster: str, node_name: str, kube: CoreV1Api):
        """Read a node, mapping 404 → NodeNotFoundException."""
        with translate_kube_errors(
            cluster,
            f"read node '{node_name}'",
            not_found=_node_not_found(cluster, node_name),
        ):
            return kube.read_node(node_name)

    @staticmethod
    def _to_taint_spec(taint) -> TaintSpec:
        """Convert a V1Taint (or fake) to a TaintSpec."""
        return TaintSpec(key=taint.key, value=taint.value, effect=taint.effect)

    def _evict_or_delete(
        self,
        cluster: str,
        kube: CoreV1Api,
        name: str,
        namespace: str,
        options: DrainOptions,
    ) -> None:
        """Evict (honour PDB) or delete (bypass PDB) a single pod."""
        grace = options.grace_period_seconds

        if options.disable_eviction:
            _logger.debug("Deleting pod | ns=%s | pod=%s", namespace, name)
            with translate_kube_errors(cluster, f"delete pod '{namespace}/{name}'"):
                try:
                    kube.delete_namespaced_pod(
                        name=name, namespace=namespace, grace_period_seconds=grace,
                    )
                except ApiException as exc:
                    if exc.status == 404:
                        return  # already gone
                    raise
        else:
            from kubernetes.client.models import V1DeleteOptions, V1Eviction, V1ObjectMeta
            _logger.debug("Evicting pod | ns=%s | pod=%s", namespace, name)
            eviction = V1Eviction(
                metadata=V1ObjectMeta(name=name, namespace=namespace),
                delete_options=V1DeleteOptions(grace_period_seconds=grace),
            )
            with translate_kube_errors(cluster, f"evict pod '{namespace}/{name}'"):
                try:
                    kube.create_namespaced_pod_eviction(
                        name=name, namespace=namespace, body=eviction,
                    )
                except ApiException as exc:
                    if exc.status == 404:
                        return  # already gone
                    if exc.status == 429:
                        raise KubeApiException(
                            f"Pod '{namespace}/{name}' cannot be evicted due to a "
                            "PodDisruptionBudget. Use disable_eviction=true to bypass.",
                            kube_status=409,
                        ) from exc
                    raise

    def _wait_for_pods_gone(
        self,
        cluster: str,
        kube: CoreV1Api,
        node_name: str,
        pod_names: set[tuple[str, str]],
        timeout_seconds: int,
    ) -> set[tuple[str, str]]:
        """Poll until the targeted pods are gone or the budget expires.

        Returns the ``(namespace, name)`` pairs still present when the budget
        ran out — empty when the node drained cleanly. Exhausting the budget is
        a normal outcome, not an error: the evictions were all accepted and a
        pod with a long ``terminationGracePeriodSeconds`` is behaving as
        configured. The caller decides what a non-empty result means.
        """
        if not pod_names:
            return set()
        deadline = time.monotonic() + timeout_seconds
        still_present: set[tuple[str, str]] = set(pod_names)
        while True:
            with translate_kube_errors(
                cluster, f"list pods on node '{node_name}' while waiting for them to drain"
            ):
                remaining = kube.list_pod_for_all_namespaces(
                    field_selector=f"spec.nodeName={node_name}"
                )

            still_present = {
                (p.metadata.namespace, p.metadata.name)
                for p in remaining.items
                if (p.metadata.namespace, p.metadata.name) in pod_names
            }
            if not still_present:
                _logger.debug("All targeted pods are gone | node=%s", node_name)
                return set()
            if time.monotonic() >= deadline:
                _logger.info(
                    "Drain wait budget spent | node=%s | still_terminating=%d",
                    node_name, len(still_present),
                )
                return still_present
            _logger.debug(
                "Waiting for %d pod(s) to terminate | node=%s",
                len(still_present), node_name,
            )
            time.sleep(2)

    @staticmethod
    def _node_status(node: V1Node) -> str:
        """Derive a node's readiness: "Ready", "NotReady" or "Unknown".

        The three map onto the Ready condition's ``status`` field, which is a
        *three-valued* string, not a boolean:

        - ``"True"``    → Ready.
        - ``"False"``   → NotReady: the kubelet is reporting and says it is
          unhealthy.
        - ``"Unknown"`` → the node controller has stopped hearing from the
          kubelet altogether (~40s of silence). The condition stays present and
          its status becomes the literal string "Unknown" — the controller does
          not remove it — so this must be read off ``cond.status`` rather than
          inferred from a missing condition.

        "Unknown" is a *worse* signal than NotReady, not a milder one: nothing
        is reporting at all. Callers gating on health must therefore treat only
        "Ready" as passing rather than listing the bad values, since any future
        status string would otherwise be admitted by default.

        A node with no conditions at all has only just registered and has not
        yet been assessed; it is reported "Unknown" for the same reason — its
        health is unestablished.
        """
        for cond in (node.status.conditions or []):
            if cond.type == "Ready":
                if cond.status == "True":
                    return "Ready"
                return "Unknown" if cond.status == "Unknown" else "NotReady"
        return "Unknown"

    @staticmethod
    def _node_to_info(node: V1Node) -> NodeInfo:
        """Convert a V1Node object to a NodeInfo response model."""
        status = NodeService._node_status(node)

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
