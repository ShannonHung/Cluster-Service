"""
tests/unit/test_kube_errors.py

translate_kube_errors — the one place a Kubernetes SDK failure becomes an
application exception (CLAUDE.md, "Exception hierarchy": the SDK never
escapes the service layer).

Three outcomes, and only three:
- an API error → KubeApiException carrying the upstream status (or the
  caller's own not-found exception, when it named one, on a 404)
- a network error → a cluster-level 503
- anything else → untouched; this is not a catch-all
"""

from __future__ import annotations

import pytest
from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app.core.exceptions import KubeApiException, NodeNotFoundException
from app.services.kube_errors import translate_kube_errors


def _node_not_found() -> NodeNotFoundException:
    return NodeNotFoundException("Node 'n1' not found in cluster 'c1'.")


def test_success_passes_through():
    with translate_kube_errors("c1", "read node 'n1'"):
        result = 42
    assert result == 42


def test_an_api_error_becomes_kube_api_exception_with_the_upstream_status():
    original = ApiException(status=403, reason="Forbidden")
    with pytest.raises(KubeApiException) as exc_info:
        with translate_kube_errors("c1", "patch node 'n1'"):
            raise original

    assert exc_info.value.http_status == 403
    assert exc_info.value.kube_status == 403
    assert str(exc_info.value) == "Failed to patch node 'n1': Forbidden"
    assert exc_info.value.__cause__ is original


def test_a_404_becomes_the_callers_not_found_exception():
    original = ApiException(status=404, reason="Not Found")
    with pytest.raises(NodeNotFoundException) as exc_info:
        with translate_kube_errors("c1", "read node 'n1'", not_found=_node_not_found):
            raise original
    assert exc_info.value.__cause__ is original


def test_a_404_without_a_not_found_exception_is_a_kube_api_error():
    """Listing calls have nothing to be 'not found' — a 404 there is an API error."""
    with pytest.raises(KubeApiException) as exc_info:
        with translate_kube_errors("c1", "list nodes in cluster 'c1'"):
            raise ApiException(status=404, reason="Not Found")
    assert exc_info.value.http_status == 404


def test_not_found_applies_only_to_404():
    with pytest.raises(KubeApiException) as exc_info:
        with translate_kube_errors("c1", "read node 'n1'", not_found=_node_not_found):
            raise ApiException(status=500, reason="Internal")
    assert exc_info.value.http_status == 500


def test_no_http_response_keeps_the_status_out():
    """status=0 is the SDK's 'no response' (#38): kube_status None, 502."""
    with pytest.raises(KubeApiException) as exc_info:
        with translate_kube_errors("c1", "read node 'n1'"):
            raise ApiException(status=0, reason="SSLError")
    assert exc_info.value.kube_status is None
    assert exc_info.value.http_status == 502


def test_a_network_error_is_a_cluster_level_503_naming_the_cluster():
    original = Urllib3HTTPError("connection refused")
    with pytest.raises(KubeApiException) as exc_info:
        with translate_kube_errors("c1", "read node 'n1'"):
            raise original

    assert exc_info.value.http_status == 503
    assert exc_info.value.cluster_level is True
    assert "c1" in str(exc_info.value)
    assert exc_info.value.__cause__ is original


def test_other_exceptions_are_not_swallowed_or_rewrapped():
    with pytest.raises(ValueError):
        with translate_kube_errors("c1", "read node 'n1'"):
            raise ValueError("a bug, not a Kubernetes failure")


def test_an_app_exception_raised_inside_passes_through_unchanged():
    """A caller's own mapping (e.g. 429 → PDB refusal) raised inside the block
    must reach the caller as-is, not be rewrapped as a generic API error."""
    pdb = KubeApiException("blocked by a PodDisruptionBudget", kube_status=409)
    with pytest.raises(KubeApiException) as exc_info:
        with translate_kube_errors("c1", "evict pod 'ns/p'"):
            raise pdb
    assert exc_info.value is pdb
