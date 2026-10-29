"""
tests/unit/test_namespace_service.py

Unit tests for NamespaceService — CoreV1Api is mocked, no cluster required.

Vocabulary follows CONTEXT.md, "Namespaces": a Terminating namespace is one
whose deletion was requested and which is still emptying itself.
"""

from __future__ import annotations

import logging
import re
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
from app.domain.models import Caller
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
        cluster="c1", name=name, labels={}, caller=Caller(account="alice", request_id="req-1"), kube=kube
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
        cluster="c1", name="team-a", labels={"team": "a"},
        caller=Caller(account="alice", request_id="r"), kube=kube,
    )

    assert kube.create_namespace.call_args.args[0].metadata.labels == {"team": "a"}
    assert result.labels == {"team": "a"}


def test_the_caller_s_annotations_are_sent_beside_the_provenance_stamps():
    kube = MagicMock()
    kube.create_namespace.side_effect = lambda body: body

    result = _svc().create_namespace(
        cluster="c1", name="team-a", labels={}, annotations={"owner": "alice"},
        caller=Caller(account="bob", request_id="req-1"), kube=kube,
    )

    sent = kube.create_namespace.call_args.args[0].metadata.annotations
    assert sent == {
        "owner": "alice",
        "cs.acme.io/created-by": "bob",
        "cs.acme.io/created-at": "2026-10-25T08:30:00Z",
        "cs.acme.io/last-request-id": "req-1",
    }
    assert result.annotations == sent


def test_an_annotation_under_the_provenance_prefix_is_refused_on_create():
    """A caller cannot forge or pre-empt the service's stamps. Refused before
    Kubernetes is called, every offender named."""
    kube = MagicMock()

    with pytest.raises(ValidationException) as info:
        _svc().create_namespace(
            cluster="c1", name="team-a", labels={},
            annotations={
                "owner": "alice",
                "cs.acme.io/created-by": "mallory",
                "x.cs.acme.io/y": "1",
            },
            caller=Caller(account="alice", request_id="r"), kube=kube,
        )

    assert "cs.acme.io/created-by" in info.value.message
    assert "x.cs.acme.io/y" in info.value.message
    assert "annotation" in info.value.message
    kube.create_namespace.assert_not_called()


def test_a_label_under_the_provenance_prefix_is_refused_on_create():
    """ADR 0002: the prefix belongs to the service — a caller cannot write under
    it with a label either. Refused before Kubernetes is called."""
    kube = MagicMock()

    with pytest.raises(ValidationException, match=re.escape("cs.acme.io/created-by")):
        _svc().create_namespace(
            cluster="c1", name="team-a", labels={"cs.acme.io/created-by": "mallory"},
            caller=Caller(account="alice", request_id="r"), kube=kube,
        )

    kube.create_namespace.assert_not_called()


def test_created_at_is_utc_whatever_the_clock_zone():
    kube = MagicMock()
    kube.create_namespace.side_effect = lambda body: body
    taipei = timezone(timedelta(hours=8))
    svc = NamespaceService(
        provenance_prefix=_PREFIX, clock=lambda: datetime(2026, 10, 25, 16, 30, tzinfo=taipei)
    )

    svc.create_namespace(
        cluster="c1", name="team-a", labels={}, caller=Caller(account="alice", request_id="r"), kube=kube
    )

    body = kube.create_namespace.call_args.args[0]
    assert body.metadata.annotations["cs.acme.io/created-at"] == "2026-10-25T08:30:00Z"


def test_a_request_without_an_id_leaves_last_request_id_out():
    """An empty annotation would read as "the request id was empty" — leave it out."""
    kube = MagicMock()
    kube.create_namespace.side_effect = lambda body: body

    _svc().create_namespace(
        cluster="c1", name="team-a", labels={}, caller=Caller(account="alice", request_id=""), kube=kube
    )

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


# ── updating labels ───────────────────────────────────────────────────────────


def _update(
    kube: MagicMock, name: str = "team-a", set_labels=None, remove_labels=None, request_id="req-2"
):
    return _svc().update_labels(
        cluster="c1", name=name, set_labels=set_labels or {}, remove_labels=remove_labels or [],
        caller=Caller(account="bob", request_id=request_id), kube=kube,
    )


def test_an_update_is_one_merge_patch_with_null_for_each_removal():
    kube = MagicMock()
    kube.patch_namespace.return_value = _ns("team-a", labels={"team": "b"})

    result = _update(kube, set_labels={"team": "b"}, remove_labels=["old"])

    name, body = kube.patch_namespace.call_args.args
    assert name == "team-a"
    assert body["metadata"]["labels"] == {"team": "b", "old": None}
    assert result.labels == {"team": "b"}
    kube.patch_namespace.assert_called_once()


def test_an_update_stamps_last_modified_and_never_created():
    kube = MagicMock()
    kube.patch_namespace.return_value = _ns("team-a")

    _update(kube, set_labels={"team": "b"})

    annotations = kube.patch_namespace.call_args.args[1]["metadata"]["annotations"]
    assert annotations == {
        "cs.acme.io/last-modified-by": "bob",
        "cs.acme.io/last-modified-at": "2026-10-25T08:30:00Z",
        "cs.acme.io/last-request-id": "req-2",
    }


def test_an_update_without_a_request_id_clears_the_previous_one():
    """Otherwise the old id would sit beside the new last-modified-* and point
    at the wrong request."""
    kube = MagicMock()
    kube.patch_namespace.return_value = _ns("team-a")

    _update(kube, set_labels={"team": "b"}, request_id="")

    annotations = kube.patch_namespace.call_args.args[1]["metadata"]["annotations"]
    assert annotations["cs.acme.io/last-request-id"] is None


def test_a_label_under_the_provenance_prefix_is_refused_on_update_set():
    kube = MagicMock()

    with pytest.raises(ValidationException, match=re.escape("cs.acme.io/last-modified-by")):
        _update(kube, set_labels={"team": "b", "cs.acme.io/last-modified-by": "mallory"})

    kube.patch_namespace.assert_not_called()


def test_a_label_under_the_provenance_prefix_is_refused_on_update_remove():
    """Removing a stamp would erase the pointer to the audit trail just as
    setting one would forge it."""
    kube = MagicMock()

    with pytest.raises(ValidationException, match=re.escape("cs.acme.io/created-by")):
        _update(kube, remove_labels=["cs.acme.io/created-by"])

    kube.patch_namespace.assert_not_called()


def test_a_subdomain_of_the_provenance_prefix_is_refused_too():
    kube = MagicMock()

    with pytest.raises(ValidationException, match=re.escape("a.cs.acme.io/x")):
        _update(kube, set_labels={"a.cs.acme.io/x": "1"})

    kube.patch_namespace.assert_not_called()


@pytest.mark.parametrize(
    "key", ["notcs.acme.io/x", "cs.acme.io.evil/x", "acme.io/x", "notcs.acme.io", "team"]
)
def test_a_key_that_only_resembles_the_provenance_prefix_is_allowed(key):
    kube = MagicMock()
    kube.patch_namespace.return_value = _ns("team-a")

    _update(kube, set_labels={key: "1"})

    kube.patch_namespace.assert_called_once()


@pytest.mark.parametrize("key", ["cs.acme.io", "a.cs.acme.io"])
def test_a_bare_key_that_is_the_provenance_domain_is_refused_everywhere(key):
    """No `/` means no prefix — but a name that is the domain itself would
    still sit under the service's domain, so it is as closed as `cs.acme.io/x`."""
    kube = MagicMock()

    with pytest.raises(ValidationException, match=re.escape(key)):
        _update(kube, set_labels={key: "1"})
    with pytest.raises(ValidationException, match=re.escape(key)):
        _update(kube, remove_labels=[key])
    with pytest.raises(ValidationException, match=re.escape(key)):
        _annotate(kube, set_annotations={key: "1"})
    with pytest.raises(ValidationException, match=re.escape(key)):
        _annotate(kube, remove_annotations=[key])
    with pytest.raises(ValidationException, match=re.escape(key)):
        _svc().create_namespace(
            cluster="c1", name="team-a", labels={}, annotations={key: "1"},
            caller=Caller(account="alice", request_id="r"), kube=kube,
        )

    kube.patch_namespace.assert_not_called()
    kube.create_namespace.assert_not_called()


def test_a_key_in_both_set_and_remove_is_named_once():
    kube = MagicMock()

    with pytest.raises(ValidationException) as info:
        _update(kube, set_labels={"cs.acme.io/a": "1"}, remove_labels=["cs.acme.io/a"])

    assert info.value.message.count("cs.acme.io/a") == 1


def test_every_key_under_the_provenance_prefix_is_named_at_once():
    kube = MagicMock()

    with pytest.raises(ValidationException) as info:
        _update(
            kube,
            set_labels={"cs.acme.io/a": "1", "team": "x"},
            remove_labels=["cs.acme.io/b"],
        )

    assert "cs.acme.io/a" in info.value.message and "cs.acme.io/b" in info.value.message
    assert "team" not in info.value.message


@pytest.mark.parametrize("name", ["default", "kube-system", "kube-public"])
def test_a_reserved_namespace_is_refused_before_kubernetes_is_called(name):
    kube = MagicMock()

    with pytest.raises(ValidationException):
        _update(kube, name=name, set_labels={"team": "a"})

    kube.patch_namespace.assert_not_called()


def test_updating_a_missing_namespace_is_namespace_not_found():
    kube = MagicMock()
    kube.patch_namespace.side_effect = ApiException(status=404, reason="Not Found")

    with pytest.raises(NamespaceNotFoundException):
        _update(kube, name="ghost", set_labels={"a": "b"})


# ── updating annotations ──────────────────────────────────────────────────────


def _annotate(
    kube: MagicMock,
    name: str = "team-a",
    set_annotations=None,
    remove_annotations=None,
    request_id="req-3",
):
    return _svc().update_annotations(
        cluster="c1", name=name, set_annotations=set_annotations or {},
        remove_annotations=remove_annotations or [],
        caller=Caller(account="carol", request_id=request_id), kube=kube,
    )


def test_an_annotation_update_is_one_merge_patch_with_null_for_each_removal():
    kube = MagicMock()
    kube.patch_namespace.return_value = _ns("team-a", annotations={"owner": "bob"})

    result = _annotate(kube, set_annotations={"owner": "bob"}, remove_annotations=["old"])

    name, body = kube.patch_namespace.call_args.args
    assert name == "team-a"
    annotations = body["metadata"]["annotations"]
    assert annotations["owner"] == "bob" and annotations["old"] is None
    assert "labels" not in body["metadata"]
    assert result.annotations == {"owner": "bob"}
    kube.patch_namespace.assert_called_once()


def test_an_annotation_update_stamps_last_modified_and_never_created():
    kube = MagicMock()
    kube.patch_namespace.return_value = _ns("team-a")

    _annotate(kube, set_annotations={"owner": "bob"})

    annotations = kube.patch_namespace.call_args.args[1]["metadata"]["annotations"]
    assert annotations == {
        "owner": "bob",
        "cs.acme.io/last-modified-by": "carol",
        "cs.acme.io/last-modified-at": "2026-10-25T08:30:00Z",
        "cs.acme.io/last-request-id": "req-3",
    }


def test_an_annotation_update_without_a_request_id_clears_the_previous_one():
    kube = MagicMock()
    kube.patch_namespace.return_value = _ns("team-a")

    _annotate(kube, set_annotations={"owner": "bob"}, request_id="")

    annotations = kube.patch_namespace.call_args.args[1]["metadata"]["annotations"]
    assert annotations["cs.acme.io/last-request-id"] is None


@pytest.mark.parametrize("name", ["default", "kube-system", "kube-public"])
def test_a_reserved_namespace_s_annotations_are_refused_before_kubernetes_is_called(name):
    kube = MagicMock()

    with pytest.raises(ValidationException):
        _annotate(kube, name=name, set_annotations={"owner": "a"})

    kube.patch_namespace.assert_not_called()


@pytest.mark.parametrize("field", ["set", "remove"])
def test_an_annotation_under_the_provenance_prefix_is_refused_on_update(field):
    """Setting one would forge a stamp; removing one would erase the pointer to
    the audit trail."""
    kube = MagicMock()
    key = "cs.acme.io/created-by"
    kwargs = {"set_annotations": {key: "x"}} if field == "set" else {"remove_annotations": [key]}

    with pytest.raises(ValidationException, match=re.escape(key)):
        _annotate(kube, **kwargs)

    kube.patch_namespace.assert_not_called()


def test_annotating_a_missing_namespace_is_not_found():
    kube = MagicMock()
    kube.patch_namespace.side_effect = ApiException(status=404, reason="Not Found")

    with pytest.raises(NamespaceNotFoundException):
        _annotate(kube, name="ghost", set_annotations={"owner": "a"})


def test_the_annotations_returned_leave_out_last_applied():
    """A read leaves out that stale copy (CONTEXT.md); so does an update."""
    kube = MagicMock()
    kube.patch_namespace.return_value = _ns(
        "team-a", annotations={"owner": "bob", _LAST_APPLIED: "{...}"}
    )

    result = _annotate(kube, set_annotations={"owner": "bob"})

    assert result.annotations == {"owner": "bob"}
