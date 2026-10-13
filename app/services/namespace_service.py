"""
app/services/namespace_service.py

NamespaceService — lists, reads and creates namespaces on a cluster.

Vocabulary follows CONTEXT.md, "Namespaces". The service never deletes a
namespace: deletion takes everything inside it, irreversibly.
"""

from __future__ import annotations

import logging
from typing import get_args

from kubernetes.client import CoreV1Api, V1Namespace, V1NamespaceList, V1ObjectMeta
from kubernetes.client.exceptions import ApiException

from app.core.exceptions import (
    NamespaceAlreadyExistsException,
    NamespaceNotFoundException,
    ValidationException,
)
from app.domain.kubernetes_models import (
    NamespaceDetailData,
    NamespaceListData,
    NamespacePhase,
    NamespaceSummary,
)
from app.services.kube_errors import translate_kube_errors

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
    """Lists namespaces, reads one, and creates one."""

    def create_namespace(
        self, cluster: str, name: str, user: str, kube: CoreV1Api
    ) -> NamespaceDetailData:
        """Create a namespace carrying nothing but its name.

        The name is validated (DNS-1123 label, not reserved) by the request
        model before this is called. No label or annotation of the service's
        own is added: who created it is the API gateway's audit trail, and this
        log line.

        Raises:
            NamespaceAlreadyExistsException: The name is taken — by a namespace
                that is there, or by a Terminating one (CONTEXT.md), which the
                message and ``detail["status"]`` tell apart.
            KubeApiException: On any other Kubernetes API failure.
        """
        with translate_kube_errors(cluster, f"create namespace '{name}' in cluster '{cluster}'"):
            try:
                ns = kube.create_namespace(V1Namespace(metadata=V1ObjectMeta(name=name)))
            except ApiException as exc:
                if exc.status != 409:
                    raise
                raise self._already_exists(cluster, name, kube) from exc

        _logger.info(
            "Created namespace | cluster=%s | name=%s | user=%s", cluster, name, user
        )
        return _to_detail(cluster, ns)

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


def _to_detail(cluster: str, ns: V1Namespace) -> NamespaceDetailData:
    annotations = {
        k: v for k, v in (ns.metadata.annotations or {}).items() if k != _LAST_APPLIED
    }
    return NamespaceDetailData(
        cluster=cluster,
        name=ns.metadata.name,
        status=_phase(ns),
        labels=ns.metadata.labels or {},
        annotations=annotations,
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
