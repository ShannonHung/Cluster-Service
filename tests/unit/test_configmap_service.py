"""
tests/unit/test_configmap_service.py

Unit tests for ConfigMapService — CoreV1Api is mocked, no cluster required.

The listing is a lower privilege than reading content (see CONTEXT.md,
"ConfigMap listing"), so the assertions that matter most here are the negative
ones: no value, and no annotation, ever reaches a listing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from kubernetes.client import V1ConfigMap, V1ConfigMapList, V1ListMeta, V1ObjectMeta
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app.core.exceptions import KubeApiException
from app.domain.kubernetes_models import ConfigMapListData
from app.services.configmap_service import PAGE_SIZE, ConfigMapService

_CREATED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def _cm(
    name: str,
    namespace: str = "default",
    data: dict[str, str] | None = None,
    binary_data: dict[str, str] | None = None,
    labels: dict[str, str] | None = None,
    annotations: dict[str, str] | None = None,
) -> V1ConfigMap:
    return V1ConfigMap(
        metadata=V1ObjectMeta(
            name=name,
            namespace=namespace,
            labels=labels,
            annotations=annotations,
            creation_timestamp=_CREATED,
        ),
        data=data,
        binary_data=binary_data,
    )


def _kube(*items: V1ConfigMap) -> MagicMock:
    kube = MagicMock()
    kube.list_namespaced_config_map.return_value = V1ConfigMapList(items=list(items))
    kube.list_config_map_for_all_namespaces.return_value = V1ConfigMapList(items=list(items))
    return kube


def _list(kube: MagicMock, namespace: str = "default", **kwargs) -> ConfigMapListData:
    return ConfigMapService().list_configmaps(
        cluster="test", namespace=namespace, kube=kube, **kwargs
    )


# ── scope of the listing call ─────────────────────────────────────────────────


def test_a_namespace_lists_only_that_namespace():
    kube = _kube(_cm("app-config"))
    result = _list(kube, namespace="default")

    assert result.cluster == "test"
    assert result.namespace == "default"
    assert [c.name for c in result.configmaps] == ["app-config"]
    kube.list_namespaced_config_map.assert_called_once_with("default", limit=PAGE_SIZE, _continue=None)
    kube.list_config_map_for_all_namespaces.assert_not_called()


def test_wildcard_lists_every_namespace():
    kube = _kube(_cm("a", namespace="default"), _cm("b", namespace="kube-system"))
    result = _list(kube, namespace="*")

    assert result.namespace == "*"
    assert {(c.namespace, c.name) for c in result.configmaps} == {
        ("default", "a"),
        ("kube-system", "b"),
    }
    kube.list_config_map_for_all_namespaces.assert_called_once_with(limit=PAGE_SIZE, _continue=None)
    kube.list_namespaced_config_map.assert_not_called()


def test_same_name_in_two_namespaces_is_two_configmaps():
    kube = _kube(_cm("shared", namespace="team-a"), _cm("shared", namespace="team-b"))
    result = _list(kube, namespace="*")

    assert {(c.namespace, c.name) for c in result.configmaps} == {
        ("team-a", "shared"),
        ("team-b", "shared"),
    }


def test_an_empty_namespace_is_an_empty_listing():
    assert _list(_kube()).configmaps == []


# ── name prefix filter ────────────────────────────────────────────────────────


def test_name_prefix_filters():
    kube = _kube(_cm("app-config"), _cm("app-flags"), _cm("db-config"))
    result = _list(kube, name_prefixes=["app-"])
    assert {c.name for c in result.configmaps} == {"app-config", "app-flags"}


def test_several_prefixes_are_ored():
    kube = _kube(_cm("app-config"), _cm("db-config"), _cm("cache-config"))
    result = _list(kube, name_prefixes=["app-", "db-"])
    assert {c.name for c in result.configmaps} == {"app-config", "db-config"}


def test_prefix_is_not_a_substring_match():
    kube = _kube(_cm("my-app-config"))
    assert _list(kube, name_prefixes=["app-"]).configmaps == []


def test_no_prefixes_means_no_filter():
    kube = _kube(_cm("a"), _cm("b"))
    assert len(_list(kube, name_prefixes=[]).configmaps) == 2


# ── what a listing shows, and what it never shows ─────────────────────────────


def test_summary_carries_shape_but_not_values():
    kube = _kube(
        _cm(
            "app-config",
            data={"LOG_LEVEL": "debug", "DB_HOST": "db.internal"},
            binary_data={"cert.der": "AAEC"},
            labels={"app": "web"},
        )
    )
    [summary] = _list(kube).configmaps

    assert summary.name == "app-config"
    assert summary.namespace == "default"
    assert summary.keys == ["DB_HOST", "LOG_LEVEL", "cert.der"]
    assert summary.labels == {"app": "web"}
    assert summary.creation_timestamp == _CREATED


def test_no_value_or_annotation_leaks_into_the_listing():
    """last-applied-configuration carries a full copy of the values — a listing
    that passed annotations through would bypass the content privilege."""
    secret_ish = "postgres://user:hunter2@db.internal"
    kube = _kube(
        _cm(
            "app-config",
            data={"DB_URL": secret_ish},
            binary_data={"blob": "c2VjcmV0"},
            annotations={_LAST_APPLIED: f'{{"data":{{"DB_URL":"{secret_ish}"}}}}'},
        )
    )
    rendered = _list(kube).model_dump_json()

    assert secret_ish not in rendered
    assert "c2VjcmV0" not in rendered
    assert _LAST_APPLIED not in rendered
    assert "annotations" not in rendered


def test_a_configmap_with_no_data_has_no_keys():
    [summary] = _list(_kube(_cm("empty"))).configmaps
    assert summary.keys == []
    assert summary.labels == {}


# ── paging ────────────────────────────────────────────────────────────────────
#
# A ConfigMap listing cannot ask Kubernetes for key names only, so every page
# arrives with its full values. Paging bounds how much of that is held at once;
# fetching everything in one response would hold the whole cluster's values.


def _page(items: list[V1ConfigMap], next_token: str | None) -> V1ConfigMapList:
    return V1ConfigMapList(items=items, metadata=V1ListMeta(_continue=next_token))


def test_follows_continue_tokens_until_the_last_page():
    kube = MagicMock()
    kube.list_config_map_for_all_namespaces.side_effect = [
        _page([_cm("a")], "token-1"),
        _page([_cm("b")], "token-2"),
        _page([_cm("c")], None),
    ]
    result = _list(kube, namespace="*")

    assert [c.name for c in result.configmaps] == ["a", "b", "c"]
    assert [c.kwargs["_continue"] for c in kube.list_config_map_for_all_namespaces.call_args_list] == [
        None,
        "token-1",
        "token-2",
    ]
    assert all(
        c.kwargs["limit"] == PAGE_SIZE
        for c in kube.list_config_map_for_all_namespaces.call_args_list
    )


def test_pages_a_single_namespace_too():
    kube = MagicMock()
    kube.list_namespaced_config_map.side_effect = [
        _page([_cm("a")], "token-1"),
        _page([_cm("b")], ""),  # the API server sends "" on the last page
    ]
    result = _list(kube, namespace="default")

    assert [c.name for c in result.configmaps] == ["a", "b"]
    assert kube.list_namespaced_config_map.call_count == 2


def test_prefix_filter_applies_across_pages():
    kube = MagicMock()
    kube.list_namespaced_config_map.side_effect = [
        _page([_cm("app-1"), _cm("db-1")], "t"),
        _page([_cm("app-2")], None),
    ]
    result = _list(kube, name_prefixes=["app-"])
    assert [c.name for c in result.configmaps] == ["app-1", "app-2"]


def test_an_error_on_a_later_page_is_still_translated():
    kube = MagicMock()
    kube.list_namespaced_config_map.side_effect = [
        _page([_cm("a")], "t"),
        ApiException(status=410, reason="Gone"),  # expired continue token
    ]
    with pytest.raises(KubeApiException) as exc_info:
        _list(kube)
    assert exc_info.value.http_status == 410


# ── errors ────────────────────────────────────────────────────────────────────


def test_api_error_becomes_kube_api_exception_with_upstream_status():
    kube = MagicMock()
    kube.list_namespaced_config_map.side_effect = ApiException(status=403, reason="Forbidden")

    with pytest.raises(KubeApiException) as exc_info:
        _list(kube)
    assert exc_info.value.http_status == 403


def test_network_error_is_a_cluster_level_503():
    kube = MagicMock()
    kube.list_config_map_for_all_namespaces.side_effect = Urllib3HTTPError("refused")

    with pytest.raises(KubeApiException) as exc_info:
        _list(kube, namespace="*")
    assert exc_info.value.http_status == 503
    assert exc_info.value.cluster_level is True
