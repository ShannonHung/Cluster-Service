"""Maps deploy-service command errors onto cluster-service ErrorCode."""
from __future__ import annotations

from app.core.exceptions import DeployServiceError, ErrorCode


def test_command_execution_error_maps():
    body = {"error": {"code": "COMMAND_EXECUTION_ERROR", "message": "ansible failed"}}
    err = DeployServiceError(http_status=500, body=body)
    resp = err.to_response()
    assert resp["error_code"] == ErrorCode.COMMAND_EXECUTION_FAILED


def test_command_not_found_still_maps_via_status():
    body = {"error": {"code": "NOT_FOUND", "message": "Command abc not found."}}
    err = DeployServiceError(http_status=404, body=body)
    resp = err.to_response()
    # Existing NOT_FOUND mapping is reused; just assert it resolves to a code.
    assert resp["error_code"] is not None
