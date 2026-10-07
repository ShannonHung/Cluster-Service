"""
tests/unit/test_kube_api_exception.py

KubeApiException must survive an ApiException that carries no status.

The SDK raises ``ApiException`` with ``status=None`` when there was no HTTP
response to read one from (some client-side and proxy failures). Every service
passes ``kube_status=exc.status`` straight through, so the exception has to
cope — otherwise a ``TypeError`` is raised *inside the except block*, the
caller gets the generic 500 instead of the structured 502, and the original
error is buried under the TypeError.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import ErrorCode, KubeApiException


def test_no_status_is_a_502_rather_than_a_crash():
    exc = KubeApiException("boom", kube_status=None)
    assert exc.http_status == 502
    assert exc.error_code == ErrorCode.KUBE_API_ERROR


def test_no_status_is_kept_as_none_not_fabricated():
    """Callers branch on kube_status (a retryable 503 vs a 403); a made-up 502
    would claim the API server answered when it did not."""
    assert KubeApiException("boom", kube_status=None).kube_status is None


@pytest.mark.parametrize("status", [400, 403, 404, 409, 500, 503])
def test_an_error_status_is_mirrored(status):
    exc = KubeApiException("boom", kube_status=status)
    assert exc.http_status == status
    assert exc.kube_status == status


@pytest.mark.parametrize("status", [0, 200, 302])
def test_a_non_error_status_falls_back_to_502(status):
    exc = KubeApiException("boom", kube_status=status)
    assert exc.http_status == 502
    assert exc.kube_status == status
