"""
app/services/namespace_service.py

NamespaceService — lists, reads, creates and relabels / re-annotates namespaces on a cluster.

Vocabulary follows CONTEXT.md, "Namespaces". The service never deletes a
namespace: deletion takes everything inside it, irreversibly.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import get_args

from kubernetes.client import CoreV1Api, V1Namespace, V1NamespaceList, V1ObjectMeta
from kubernetes.client.exceptions import ApiException

from app.core.exceptions import (
    NamespaceAlreadyExistsException,
    NamespaceNotFoundException,
    ValidationException,
)
from app.domain.kubernetes_models import (
    NamespaceAnnotationsData,
    NamespaceDetailData,
    NamespaceLabelsData,
    NamespaceListData,
    NamespacePhase,
    NamespaceSummary,
    is_reserved_namespace_name,
)
from app.domain.label_rules import provenance_keys
from app.domain.models import Caller
from app.services.kube_errors import translate_kube_errors
from app.services.kube_patch import key_merge_patch

_logger = logging.getLogger(__name__)

# A stale copy of the manifest as of the last `kubectl apply` — not the current
# state, so it is left out of a read (as for a ConfigMap).
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"

# Namespaces per request to the API server, as for ConfigMaps. The caller never
# sees paging: the listing follows every continue token and returns everything.
PAGE_SIZE = 500

# Lower-cased filter value → phase. An allowlist, so a typo in a filter is a 422
# rather than a silently empty listing.
_PHASES: dict[str, NamespacePhase] = {p.lower(): p for p in get_args(NamespacePhase)}


class NamespaceService:
    """Lists namespaces, reads one, and creates one.

    ``provenance_prefix`` is the domain the service's provenance annotations
    live under (ADR 0002); ``clock`` is injectable so their timestamps can be
    asserted.
    """

    def __init__(
        self,
        provenance_prefix: str,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._prefix = provenance_prefix
        self._clock = clock

    def create_namespace(
        self,
        cluster: str,
        name: str,
        labels: dict[str, str],
        caller: Caller,
        kube: CoreV1Api,
        annotations: dict[str, str] | None = None,
    ) -> NamespaceDetailData:
        """Create a namespace stamped with its provenance annotations.

        The name and ``labels`` are validated (DNS-1123 label, not reserved;
        no Reserved label key) by the request model before this is called. The
        annotations say who (the caller's authenticated account), when, and in
        which request — a pointer back to the audit trail, not an audit record
        (ADR 0002).

        Raises:
            ValidationException: ``annotations`` has a key under the provenance
                annotation domain, which the service alone writes.
            NamespaceAlreadyExistsException: The name is taken — by a namespace
                that is there, or by a Terminating one (CONTEXT.md), which the
                message and ``detail["status"]`` tell apart.
            KubeApiException: On any other Kubernetes API failure.
        """
        self._refuse_provenance_keys(list(annotations or {}))
        self._refuse_provenance_keys(list(labels), kind="Label")
        with translate_kube_errors(cluster, f"create namespace '{name}' in cluster '{cluster}'"):
            try:
                ns = kube.create_namespace(
                    V1Namespace(
                        metadata=V1ObjectMeta(
                            name=name,
                            labels=labels or None,
                            # Nothing to clear on a new namespace, so an absent
                            # request id is left out rather than sent as null.
                            annotations={
                                **(annotations or {}),
                                **{
                                    k: v
                                    for k, v in self._provenance("created", caller).items()
                                    if v is not None
                                },
                            },
                        )
                    )
                )
            except ApiException as exc:
                if exc.status != 409:
                    raise
                raise self._already_exists(cluster, name, kube) from exc

        _logger.info(
            "Created namespace | cluster=%s | name=%s | user=%s", cluster, name, caller.account
        )
        return _to_detail(cluster, ns)

    def update_labels(
        self,
        cluster: str,
        name: str,
        set_labels: dict[str, str],
        remove_labels: list[str],
        caller: Caller,
        kube: CoreV1Api,
    ) -> NamespaceLabelsData:
        """Add / overwrite ``set_labels`` and delete ``remove_labels`` in one
        patch, stamping ``last-modified-*`` and ``last-request-id`` alongside
        (ADR 0002); ``created-*`` is never touched. Keys are validated by the
        request model; a key absent from the namespace is a no-op.

        Raises:
            ValidationException: ``name`` is a Reserved namespace name, or a
                key is under the provenance domain (in ``set`` or ``remove``).
            NamespaceNotFoundException: The namespace does not exist.
            KubeApiException: On any other Kubernetes API failure.
        """
        if is_reserved_namespace_name(name):
            raise ValidationException(
                f"Namespace '{name}' is reserved for Kubernetes; its labels cannot be "
                "changed through this service.",
            )
        self._refuse_provenance_keys([*set_labels, *remove_labels], kind="Label")
        body = {
            "metadata": {
                "labels": key_merge_patch(set_labels, remove_labels),
                "annotations": self._provenance("last-modified", caller),
            }
        }
        with translate_kube_errors(
            cluster,
            f"patch labels on namespace '{name}' of cluster '{cluster}'",
            not_found=lambda: NamespaceNotFoundException(
                f"Namespace '{name}' not found in cluster '{cluster}'.",
            ),
        ):
            ns = kube.patch_namespace(name, body)

        _logger.info(
            "Patched namespace labels | cluster=%s | name=%s | set=%s | remove=%s | user=%s",
            cluster, name, sorted(set_labels), sorted(remove_labels), caller.account,
        )
        return NamespaceLabelsData(cluster=cluster, name=name, labels=ns.metadata.labels or {})

    def update_annotations(
        self,
        cluster: str,
        name: str,
        set_annotations: dict[str, str],
        remove_annotations: list[str],
        caller: Caller,
        kube: CoreV1Api,
    ) -> NamespaceAnnotationsData:
        """Add / overwrite ``set_annotations`` and delete ``remove_annotations``
        in one patch, stamping ``last-modified-*`` and ``last-request-id``
        alongside (ADR 0002). The provenance annotation domain is refused in
        both ``set`` and ``remove``: only the service writes it, so a caller can
        neither forge nor erase a stamp.

        Raises:
            ValidationException: ``name`` is a Reserved namespace name, or a
                key is under the provenance annotation domain.
            NamespaceNotFoundException: The namespace does not exist.
            KubeApiException: On any other Kubernetes API failure.
        """
        if is_reserved_namespace_name(name):
            raise ValidationException(
                f"Namespace '{name}' is reserved for Kubernetes; its annotations cannot be "
                "changed through this service.",
            )
        self._refuse_provenance_keys([*set_annotations, *remove_annotations])
        body = {
            "metadata": {
                "annotations": {
                    **key_merge_patch(set_annotations, remove_annotations),
                    **self._provenance("last-modified", caller),
                }
            }
        }
        with translate_kube_errors(
            cluster,
            f"patch annotations on namespace '{name}' of cluster '{cluster}'",
            not_found=lambda: NamespaceNotFoundException(
                f"Namespace '{name}' not found in cluster '{cluster}'.",
            ),
        ):
            ns = kube.patch_namespace(name, body)

        _logger.info(
            "Patched namespace annotations | cluster=%s | name=%s | set=%s | remove=%s | user=%s",
            cluster, name, sorted(set_annotations), sorted(remove_annotations), caller.account,
        )
        return NamespaceAnnotationsData(
            cluster=cluster, name=name, annotations=_visible_annotations(ns)
        )

    def list_namespaces(
        self,
        cluster: str,
        kube: CoreV1Api,
        name_prefixes: list[str] | None = None,
        statuses: list[str] | None = None,
        label_selector: str | None = None,
    ) -> NamespaceListData:
        """List namespaces, filtered.

        ``name_prefixes`` (prefix match) and ``statuses`` (phase, case-
        insensitive) are each OR'd within and AND'd together, as for pods;
        empty/None does not filter. ``label_selector`` is evaluated by
        Kubernetes.

        Raises:
            ValidationException: An unknown status, or a label selector
                Kubernetes rejects as malformed.
            KubeApiException: On any other Kubernetes API failure.
        """
        unknown = [s for s in statuses or [] if s.lower() not in _PHASES]
        if unknown:
            raise ValidationException(
                f"Unknown namespace status {unknown}; expected one of "
                f"{sorted(_PHASES.values())}.",
            )
        phases = {_PHASES[s.lower()] for s in statuses} if statuses else None
        prefixes = tuple(name_prefixes) if name_prefixes else None

        namespaces: list[NamespaceSummary] = []
        token: str | None = None
        while True:
            page = self._fetch_page(cluster, kube, label_selector, token)
            namespaces.extend(
                _to_summary(ns)
                for ns in page.items
                if (prefixes is None or ns.metadata.name.startswith(prefixes))
                and (phases is None or _phase(ns) in phases)
            )
            token = page.metadata._continue if page.metadata else None
            if not token:  # the API server sends "" (or nothing) on the last page
                break

        _logger.info("Listed %d namespace(s) | cluster=%s", len(namespaces), cluster)
        return NamespaceListData(cluster=cluster, namespaces=namespaces)

    def get_namespace(self, cluster: str, name: str, kube: CoreV1Api) -> NamespaceDetailData:
        """Read one namespace.

        Raises:
            NamespaceNotFoundException: The namespace does not exist.
            KubeApiException: On any other Kubernetes API failure.
        """
        with translate_kube_errors(
            cluster,
            f"read namespace '{name}' of cluster '{cluster}'",
            not_found=lambda: NamespaceNotFoundException(
                f"Namespace '{name}' not found in cluster '{cluster}'.",
            ),
        ):
            ns = kube.read_namespace(name)

        _logger.info("Read namespace | cluster=%s | name=%s", cluster, name)
        return _to_detail(cluster, ns)

    def _refuse_provenance_keys(self, keys: list[str], kind: str = "Annotation") -> None:
        reserved = provenance_keys(keys, self._prefix)
        if reserved:
            raise ValidationException(
                f"{kind} keys {reserved} are under '{self._prefix}', which the "
                "service writes itself; they cannot be set or removed.",
            )

    def _provenance(self, verb: str, caller: Caller) -> dict[str, str | None]:
        """``<prefix>/<verb>-by``, ``<prefix>/<verb>-at`` and
        ``<prefix>/last-request-id``, as merge-patch values. With no request id
        the last is ``None`` — it deletes a previous id, which would otherwise
        sit beside the new stamps and point at the wrong request."""
        at = self._clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            f"{self._prefix}/{verb}-by": caller.account,
            f"{self._prefix}/{verb}-at": at,
            f"{self._prefix}/last-request-id": caller.request_id or None,
        }

    @staticmethod
    def _already_exists(
        cluster: str, name: str, kube: CoreV1Api
    ) -> NamespaceAlreadyExistsException:
        """Kubernetes answers 409 both for a namespace that is there and for a
        Terminating one, with only its message text to tell them apart. That
        text is not a contract, so the phase is read instead."""
        with translate_kube_errors(cluster, f"read namespace '{name}' of cluster '{cluster}'"):
            try:
                phase: NamespacePhase | None = _phase(kube.read_namespace(name))
            except ApiException as exc:
                if exc.status != 404:
                    raise
                phase = None  # finished deleting in between; the name was still taken

        if phase == "Terminating":
            message = (
                f"Namespace '{name}' in cluster '{cluster}' is being deleted; "
                "its name cannot be reused until the deletion finishes."
            )
        else:
            message = f"Namespace '{name}' already exists in cluster '{cluster}'."
        return NamespaceAlreadyExistsException(message, detail={"name": name, "status": phase})

    @staticmethod
    def _fetch_page(
        cluster: str, kube: CoreV1Api, label_selector: str | None, token: str | None
    ) -> V1NamespaceList:
        with translate_kube_errors(cluster, f"list namespaces of cluster '{cluster}'"):
            try:
                return kube.list_namespace(
                    label_selector=label_selector, limit=PAGE_SIZE, _continue=token
                )
            except ApiException as exc:
                # The only caller input Kubernetes evaluates here is the
                # selector, so a 400 is the caller's mistake, not the cluster's.
                if exc.status == 400:
                    raise ValidationException(
                        f"Invalid label selector '{label_selector}': {exc.reason}",
                    ) from exc
                raise


def _phase(ns: V1Namespace) -> NamespacePhase:
    # The API server defaults a missing phase to Active.
    return ns.status.phase if ns.status and ns.status.phase else "Active"


def _visible_annotations(ns: V1Namespace) -> dict[str, str]:
    return {k: v for k, v in (ns.metadata.annotations or {}).items() if k != _LAST_APPLIED}


def _to_detail(cluster: str, ns: V1Namespace) -> NamespaceDetailData:
    return NamespaceDetailData(
        cluster=cluster,
        name=ns.metadata.name,
        status=_phase(ns),
        labels=ns.metadata.labels or {},
        annotations=_visible_annotations(ns),
        creation_timestamp=ns.metadata.creation_timestamp,
        deletion_timestamp=ns.metadata.deletion_timestamp,
    )


def _to_summary(ns: V1Namespace) -> NamespaceSummary:
    return NamespaceSummary(
        name=ns.metadata.name,
        status=_phase(ns),
        labels=ns.metadata.labels or {},
        creation_timestamp=ns.metadata.creation_timestamp,
    )
