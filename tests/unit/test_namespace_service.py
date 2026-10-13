"""
tests/unit/test_namespace_service.py

Unit tests for NamespaceService — CoreV1Api is mocked, no cluster required.

Vocabulary follows CONTEXT.md, "Namespaces": a Terminating namespace is one
whose deletion was requested and which is still emptying itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
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

from app.core.exceptions import NamespaceNotFoundException, ValidationException
from app.services.namespace_service import PAGE_SIZE, NamespaceService

_CREATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_DELETED = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


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

    result = NamespaceService().get_namespace(cluster="c1", name="team-a", kube=kube)

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

    result = NamespaceService().get_namespace(cluster="c1", name="legacy", kube=kube)

    assert result.status == "Terminating"
    assert result.deletion_timestamp == _DELETED


def test_a_missing_namespace_is_namespace_not_found():
    kube = MagicMock()
    kube.read_namespace.side_effect = ApiException(status=404, reason="Not Found")

    with pytest.raises(NamespaceNotFoundException) as info:
        NamespaceService().get_namespace(cluster="c1", name="ghost", kube=kube)

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

    result = NamespaceService().list_namespaces(cluster="c1", kube=kube)

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

    result = NamespaceService().list_namespaces(cluster="c1", kube=kube)

    assert _names(result) == ["a", "b"]
    assert [c.kwargs["_continue"] for c in kube.list_namespace.call_args_list] == [None, "next"]
    assert all(c.kwargs["limit"] == PAGE_SIZE for c in kube.list_namespace.call_args_list)


def test_name_prefixes_are_ored():
    kube = _kube(_ns("team-a"), _ns("team-b"), _ns("ops-x"), _ns("default"))

    result = NamespaceService().list_namespaces(
        cluster="c1", kube=kube, name_prefixes=["team-", "ops-"]
    )

    assert _names(result) == ["team-a", "team-b", "ops-x"]


def test_status_filter_ignores_case():
    kube = _kube(_ns("a"), _ns("b", phase="Terminating"))

    result = NamespaceService().list_namespaces(
        cluster="c1", kube=kube, statuses=["terminating"]
    )

    assert _names(result) == ["b"]


def test_filters_are_anded():
    kube = _kube(_ns("team-a"), _ns("team-b", phase="Terminating"), _ns("ops", phase="Terminating"))

    result = NamespaceService().list_namespaces(
        cluster="c1", kube=kube, name_prefixes=["team-"], statuses=["Terminating"]
    )

    assert _names(result) == ["team-b"]


def test_an_unknown_status_is_refused_before_kubernetes_is_called():
    kube = _kube(_ns("a"))

    with pytest.raises(ValidationException) as info:
        NamespaceService().list_namespaces(cluster="c1", kube=kube, statuses=["Deleted"])

    assert "Deleted" in info.value.message
    kube.list_namespace.assert_not_called()


def test_the_label_selector_is_left_to_kubernetes():
    kube = _kube(_ns("a"))

    NamespaceService().list_namespaces(cluster="c1", kube=kube, label_selector="team=a")

    assert kube.list_namespace.call_args.kwargs["label_selector"] == "team=a"


def test_a_malformed_label_selector_is_a_validation_error():
    kube = MagicMock()
    kube.list_namespace.side_effect = ApiException(status=400, reason="Bad Request")

    with pytest.raises(ValidationException) as info:
        NamespaceService().list_namespaces(cluster="c1", kube=kube, label_selector="=oops")

    assert info.value.http_status == 422
    assert "=oops" in info.value.message
