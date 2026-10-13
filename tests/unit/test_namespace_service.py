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
from kubernetes.client import V1Namespace, V1NamespaceStatus, V1ObjectMeta
from kubernetes.client.exceptions import ApiException

from app.core.exceptions import NamespaceNotFoundException
from app.services.namespace_service import NamespaceService

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
