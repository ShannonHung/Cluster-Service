"""
tests/unit/test_namespace_service.py

Unit tests for NamespaceService — CoreV1Api is mocked, no cluster required.

Vocabulary follows CONTEXT.md, "Namespaces": a Terminating namespace is one
whose deletion was requested and which is still emptying itself.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from kubernetes.client import (
    V1ListMeta,
    V1Namespace,
    V1NamespaceList,
    V1NamespaceStatus,
    V1ObjectMeta,
)
from kubernetes.client.exceptions import ApiException

from app.core.exceptions import (
    KubeApiException,
    NamespaceAlreadyExistsException,
    NamespaceNotFoundException,
    ValidationException,
)
from app.services.namespace_service import PAGE_SIZE, NamespaceService

_CREATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_DELETED = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
_PREFIX = "cs.acme.io"
_NOW = datetime(2026, 10, 25, 8, 30, 0, tzinfo=timezone.utc)


def _svc() -> NamespaceService:
    return NamespaceService(provenance_prefix=_PREFIX, clock=lambda: _NOW)


def _ns(
    name: str,
    phase: str = "Active",
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
    deletion_timestamp: datetime | None = None,
) -> V1Namespace:
    return V1Namespace(
        metadata=V1ObjectMeta(
            name=name,
            labels=labels,
            annotations=annotations,
            creation_timestamp=_CREATED,
            deletion_timestamp=deletion_timestamp,
        ),
        status=V1NamespaceStatus(phase=phase),
    )


# ── reading one namespace ─────────────────────────────────────────────────────


def test_reads_one_namespace():
    kube = MagicMock()
    kube.read_namespace.return_value = _ns(
        "team-a",
        labels={"team": "a"},
        annotations={"owner": "ops", _LAST_APPLIED: '{"kind": "Namespace"}'},
    )

    result = _svc().get_namespace(cluster="c1", name="team-a", kube=kube)

    assert result.cluster == "c1"
    assert result.name == "team-a"
    assert result.status == "Active"
    assert result.labels == {"team": "a"}
    assert result.annotations == {"owner": "ops"}
    assert result.creation_timestamp == _CREATED
    assert result.deletion_timestamp is None
    kube.read_namespace.assert_called_once_with("team-a")


def test_a_terminating_namespace_carries_its_deletion_time():
    kube = MagicMock()
    kube.read_namespace.return_value = _ns(
        "legacy", phase="Terminating", deletion_timestamp=_DELETED
    )

    result = _svc().get_namespace(cluster="c1", name="legacy", kube=kube)

    assert result.status == "Terminating"
    assert result.deletion_timestamp == _DELETED


def test_a_missing_namespace_is_namespace_not_found():
    kube = MagicMock()
    kube.read_namespace.side_effect = ApiException(status=404, reason="Not Found")

    with pytest.raises(NamespaceNotFoundException) as info:
        _svc().get_namespace(cluster="c1", name="ghost", kube=kube)

    assert info.value.http_status == 404
    assert info.value.error_code == "NAMESPACE_NOT_FOUND"
    assert "ghost" in info.value.message


# ── listing ───────────────────────────────────────────────────────────────────


def _page(*items: V1Namespace, token: str | None = None) -> V1NamespaceList:
    return V1NamespaceList(items=list(items), metadata=V1ListMeta(_continue=token))


def _kube(*items: V1Namespace) -> MagicMock:
    kube = MagicMock()
    kube.list_namespace.return_value = _page(*items)
    return kube


def _names(result) -> list[str]:
    return [n.name for n in result.namespaces]


def test_lists_every_namespace_with_its_shape():
    kube = _kube(_ns("default", labels={"a": "b"}), _ns("legacy", phase="Terminating"))

    result = _svc().list_namespaces(cluster="c1", kube=kube)

    assert result.cluster == "c1"
    first, second = result.namespaces
    assert (first.name, first.status, first.labels, first.creation_timestamp) == (
        "default", "Active", {"a": "b"}, _CREATED,
    )
    assert (second.name, second.status) == ("legacy", "Terminating")


def test_follows_continue_tokens_until_the_last_page():
    kube = MagicMock()
    kube.list_namespace.side_effect = [
        _page(_ns("a"), token="next"),
        _page(_ns("b"), token=""),  # the API server sends "" on the last page
    ]

    result = _svc().list_namespaces(cluster="c1", kube=kube)

    assert _names(result) == ["a", "b"]
    assert [c.kwargs["_continue"] for c in kube.list_namespace.call_args_list] == [None, "next"]
    assert all(c.kwargs["limit"] == PAGE_SIZE for c in kube.list_namespace.call_args_list)


def test_name_prefixes_are_ored():
    kube = _kube(_ns("team-a"), _ns("team-b"), _ns("ops-x"), _ns("default"))

    result = _svc().list_namespaces(
        cluster="c1", kube=kube, name_prefixes=["team-", "ops-"]
    )

    assert _names(result) == ["team-a", "team-b", "ops-x"]


def test_status_filter_ignores_case():
    kube = _kube(_ns("a"), _ns("b", phase="Terminating"))

    result = _svc().list_namespaces(
        cluster="c1", kube=kube, statuses=["terminating"]
    )

    assert _names(result) == ["b"]


def test_filters_are_anded():
    kube = _kube(_ns("team-a"), _ns("team-b", phase="Terminating"), _ns("ops", phase="Terminating"))

    result = _svc().list_namespaces(
        cluster="c1", kube=kube, name_prefixes=["team-"], statuses=["Terminating"]
    )

    assert _names(result) == ["team-b"]


def test_an_unknown_status_is_refused_before_kubernetes_is_called():
    kube = _kube(_ns("a"))

    with pytest.raises(ValidationException) as info:
        _svc().list_namespaces(cluster="c1", kube=kube, statuses=["Deleted"])

    assert "Deleted" in info.value.message
    kube.list_namespace.assert_not_called()


def test_the_label_selector_is_left_to_kubernetes():
    kube = _kube(_ns("a"))

    _svc().list_namespaces(cluster="c1", kube=kube, label_selector="team=a")

    assert kube.list_namespace.call_args.kwargs["label_selector"] == "team=a"


def test_a_malformed_label_selector_is_a_validation_error():
    kube = MagicMock()
    kube.list_namespace.side_effect = ApiException(status=400, reason="Bad Request")

    with pytest.raises(ValidationException) as info:
        _svc().list_namespaces(cluster="c1", kube=kube, label_selector="=oops")

    assert info.value.http_status == 422
    assert "=oops" in info.value.message


# ── creating ──────────────────────────────────────────────────────────────────


def _create(kube: MagicMock, name: str = "team-a"):
    return _svc().create_namespace(
        cluster="c1", name=name, user="alice", request_id="req-1", kube=kube
    )


def test_creates_a_namespace_stamped_with_its_provenance():
    """ADR 0002: who, when and in which request — under the configured prefix.
    No label of the service's own."""
    kube = MagicMock()
    kube.create_namespace.side_effect = lambda body: body

    result = _create(kube)

    body = kube.create_namespace.call_args.args[0]
    assert body.metadata.name == "team-a"
    assert not body.metadata.labels
    assert body.metadata.annotations == {
        "cs.acme.io/created-by": "alice",
        "cs.acme.io/created-at": "2026-10-25T08:30:00Z",
        "cs.acme.io/last-request-id": "req-1",
    }
    assert result.annotations == body.metadata.annotations


def test_the_caller_s_labels_are_sent_with_the_namespace():
    kube = MagicMock()
    kube.create_namespace.side_effect = lambda body: body

    result = _svc().create_namespace(
        cluster="c1", name="team-a", user="alice", request_id="r", kube=kube,
        labels={"team": "a"},
    )

    assert kube.create_namespace.call_args.args[0].metadata.labels == {"team": "a"}
    assert result.labels == {"team": "a"}


def test_created_at_is_utc_whatever_the_clock_zone():
    kube = MagicMock()
    kube.create_namespace.side_effect = lambda body: body
    taipei = timezone(timedelta(hours=8))
    svc = NamespaceService(
        provenance_prefix=_PREFIX, clock=lambda: datetime(2026, 10, 25, 16, 30, tzinfo=taipei)
    )

    svc.create_namespace(cluster="c1", name="team-a", user="alice", request_id="r", kube=kube)

    body = kube.create_namespace.call_args.args[0]
    assert body.metadata.annotations["cs.acme.io/created-at"] == "2026-10-25T08:30:00Z"


def test_a_request_without_an_id_leaves_last_request_id_out():
    """An empty annotation would read as "the request id was empty" — leave it out."""
    kube = MagicMock()
    kube.create_namespace.side_effect = lambda body: body

    _svc().create_namespace(cluster="c1", name="team-a", user="alice", request_id="", kube=kube)

    body = kube.create_namespace.call_args.args[0]
    assert "cs.acme.io/last-request-id" not in body.metadata.annotations


def test_a_creation_is_logged_with_who_asked(caplog):
    kube = MagicMock()
    kube.create_namespace.return_value = _ns("team-a")

    with caplog.at_level(logging.INFO, logger="app.services.namespace_service"):
        _create(kube)

    line = next(r.getMessage() for r in caplog.records if "Created namespace" in r.getMessage())
    assert "cluster=c1" in line and "name=team-a" in line and "user=alice" in line


def _conflict(kube: MagicMock, existing: V1Namespace) -> None:
    kube.create_namespace.side_effect = ApiException(status=409, reason="Conflict")
    kube.read_namespace.return_value = existing


def test_an_existing_name_is_namespace_already_exists():
    kube = MagicMock()
    _conflict(kube, _ns("team-a"))

    with pytest.raises(NamespaceAlreadyExistsException) as info:
        _create(kube)

    assert info.value.http_status == 409
    assert info.value.error_code == "NAMESPACE_ALREADY_EXISTS"
    assert info.value.detail["status"] == "Active"
    assert "already exists" in info.value.message


def test_a_terminating_namespace_is_reported_as_being_deleted():
    """Told apart by the namespace's phase, never by Kubernetes' message text."""
    kube = MagicMock()
    _conflict(kube, _ns("team-a", phase="Terminating", deletion_timestamp=_DELETED))

    with pytest.raises(NamespaceAlreadyExistsException) as info:
        _create(kube)

    assert info.value.error_code == "NAMESPACE_ALREADY_EXISTS"
    assert info.value.detail["status"] == "Terminating"
    assert "being deleted" in info.value.message
    kube.read_namespace.assert_called_once_with("team-a")


def test_a_namespace_gone_between_the_conflict_and_the_read_still_conflicts():
    kube = MagicMock()
    kube.create_namespace.side_effect = ApiException(status=409, reason="Conflict")
    kube.read_namespace.side_effect = ApiException(status=404, reason="Not Found")

    with pytest.raises(NamespaceAlreadyExistsException) as info:
        _create(kube)

    assert info.value.detail["status"] is None


def test_any_other_failure_is_a_kube_api_error():
    kube = MagicMock()
    kube.create_namespace.side_effect = ApiException(status=403, reason="Forbidden")

    with pytest.raises(KubeApiException) as info:
        _create(kube)

    assert info.value.kube_status == 403
