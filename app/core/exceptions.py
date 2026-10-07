"""
app/core/exceptions.py

Custom application exception hierarchy.

Design principles:
  - Every exception carries: error_code, http_status, log_level, source_function
  - A global handler in main.py catches BaseAppException and returns:
      {"error": {"code": "...", "message": "..."}, "request_id": "..."}
  - Unhandled exceptions fall through to a catch-all handler that logs
    the full traceback without leaking internals to the client.

Upstream-service errors (ServiceError):
  - All errors from external services are normalised to a 4-field dict:
      {"error_code": "...", "message": "...", "service": "...", "details": {...}}
  - ErrorCode enum keeps error identifiers free of magic strings.
  - DeployServiceError maps deploy-service HTTP responses to the schema
    and inherits from BaseAppException for consistent handling.
"""

from __future__ import annotations

import inspect
import logging
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


# ──────────────────────────────────────────────────────────────────────────────
# ErrorCode Enum  — business-level, NOT coupled to HTTP status codes
# ──────────────────────────────────────────────────────────────────────────────

class ErrorCode(StrEnum):
    """Machine-readable business error identifiers.

    Kept separate from HTTP status so that a single HTTP status (e.g. 404) can
    have multiple, semantically distinct error codes (PIPELINE_NOT_FOUND,
    USER_NOT_FOUND, …).
    """

    # ── Pipeline ──────────────────────────────────────────────────────────────
    PIPELINE_NOT_FOUND         = "PIPELINE_NOT_FOUND"
    PIPELINE_CONFLICT          = "PIPELINE_CONFLICT"
    PIPELINE_TRIGGER_FAILED    = "PIPELINE_TRIGGER_FAILED"
    PIPELINE_CANCEL_FAILED     = "PIPELINE_CANCEL_FAILED"
    PIPELINE_RETRY_FAILED      = "PIPELINE_RETRY_FAILED"

    # ── Command (SSH command proxy) ───────────────────────────────────────────
    COMMAND_EXECUTION_FAILED   = "COMMAND_EXECUTION_FAILED"
    SCRIPT_VERSION_MISMATCH    = "SCRIPT_VERSION_MISMATCH"

    # ── Deploy-service level ──────────────────────────────────────────────────
    DEPLOY_SERVICE_UNAVAILABLE = "DEPLOY_SERVICE_UNAVAILABLE"
    DEPLOY_SERVICE_AUTH_ERROR  = "DEPLOY_SERVICE_AUTH_ERROR"
    DEPLOY_SERVICE_FORBIDDEN   = "DEPLOY_SERVICE_FORBIDDEN"

    # ── Inventory ─────────────────────────────────────────────────────────────
    INVENTORY_NOT_FOUND        = "INVENTORY_NOT_FOUND"

    # ── Generic upstream ──────────────────────────────────────────────────────
    UPSTREAM_ERROR             = "UPSTREAM_ERROR"

    # ── Kubernetes cluster ────────────────────────────────────────────────────
    CLUSTER_NOT_FOUND          = "CLUSTER_NOT_FOUND"
    NODE_NOT_FOUND             = "NODE_NOT_FOUND"
    NODE_OPERATION_FAILED      = "NODE_OPERATION_FAILED"
    KUBE_API_ERROR             = "KUBE_API_ERROR"
    DRAIN_BLOCKED              = "DRAIN_BLOCKED"
    NODE_NOT_READY             = "NODE_NOT_READY"
    CONFIGMAP_NOT_FOUND        = "CONFIGMAP_NOT_FOUND"


# ──────────────────────────────────────────────────────────────────────────────
# ServiceError — abstract interface for upstream service error adapters
# ──────────────────────────────────────────────────────────────────────────────

class ServiceError(ABC):
    """Contract that every upstream-service error adapter must implement.

    The unified schema:
        {
            "error_code": "<ErrorCode>",
            "message":    "<human-readable>",
            "service":    "<service-name>",
            "details":    {<service-specific data>}
        }
    """

    @abstractmethod
    def to_response(self) -> dict:
        """Return the 4-field unified error payload."""


# ──────────────────────────────────────────────────────────────────────────────
# Base
# ──────────────────────────────────────────────────────────────────────────────

class BaseAppException(Exception):
    """Base class for all application-specific exceptions."""

    http_status: int = 500
    error_code: str = "INTERNAL_ERROR"
    log_level: int = logging.ERROR

    def __init__(
        self,
        message: str,
        *,
        source_function: str = "",
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

        # Auto-detect caller's qualified name when not provided.
        if source_function:
            self.source_function = source_function
        else:
            frame = inspect.currentframe()
            caller = frame.f_back if frame is not None else None
            if caller is not None and 'self' in caller.f_locals: 
                src_func = caller.f_locals['self'].__class__.__name__ \
                    + "." + caller.f_code.co_name
            elif caller is not None:
                src_func = caller.f_code.co_filename \
                    + ":" + str(caller.f_lineno)
            else:
                src_func = "unknown"
            self.source_function = src_func

    def log(self, logger: logging.Logger) -> None:
        """Emit a structured log entry at the appropriate level."""
        logger.log(
            self.log_level,
            "[%s] %s | source=%s | detail=%s",
            self.error_code,
            self.message,
            self.source_function or "unknown",
            self.detail,
        )


# ───────────────────────────────────────────────────────────────────────────
# Upstream Service Exceptions
# ───────────────────────────────────────────────────────────────────────────

class UpstreamServiceException(BaseAppException):
    """Base class for exceptions originating from upstream services."""

    http_status = 502
    error_code = "UPSTREAM_ERROR"
    log_level = logging.ERROR


# ──────────────────────────────────────────────────────────────────────────────
# DeployServiceError — adapts deploy-service HTTP error → unified schema
# ──────────────────────────────────────────────────────────────────────────────

# Mapping from deploy-service's error.code strings to our ErrorCode enum.
_DEPLOY_CODE_MAP: dict[str, ErrorCode] = {
    "NOT_FOUND":  ErrorCode.PIPELINE_NOT_FOUND,
    "CONFLICT":   ErrorCode.PIPELINE_CONFLICT,
    "AUTH_ERROR": ErrorCode.DEPLOY_SERVICE_AUTH_ERROR,
    "FORBIDDEN":  ErrorCode.DEPLOY_SERVICE_FORBIDDEN,
    "COMMAND_EXECUTION_ERROR": ErrorCode.COMMAND_EXECUTION_FAILED,
    "SCRIPT_VERSION_MISMATCH": ErrorCode.SCRIPT_VERSION_MISMATCH,
}

# Fallback: map HTTP status → ErrorCode when response body is absent or unrecognised.
_DEPLOY_STATUS_MAP: dict[int, ErrorCode] = {
    401: ErrorCode.DEPLOY_SERVICE_AUTH_ERROR,
    403: ErrorCode.DEPLOY_SERVICE_FORBIDDEN,
    404: ErrorCode.PIPELINE_NOT_FOUND,
    409: ErrorCode.PIPELINE_CONFLICT,
    412: ErrorCode.SCRIPT_VERSION_MISMATCH,
    503: ErrorCode.DEPLOY_SERVICE_UNAVAILABLE,
}


class DeployServiceError(UpstreamServiceException, ServiceError):
    """Exception raised for errors returned by deploy-service.

    Adapts a deploy-service HTTP error response to the unified 4-field schema.
    deploy-service error body shape:
        {"error": {"code": "...", "message": "...", "detail": ...}, "request_id": "..."}
    """

    SERVICE_NAME = "deploy-service"

    def __init__(self, http_status: int, body: dict) -> None:
        self.upstream_status = http_status
        error_block: dict = body.get("error", {}) if isinstance(body, dict) else {}
        raw_code: str = error_block.get("code", "")
        self._error_code: ErrorCode = (
            _DEPLOY_CODE_MAP.get(raw_code)
            or _DEPLOY_STATUS_MAP.get(http_status)
            or ErrorCode.UPSTREAM_ERROR
        )
        self._message: str = error_block.get(
            "message", f"deploy-service returned HTTP {http_status}."
        )
        self._details: Any = error_block.get("detail")
        
        # Populate BaseAppException fields
        super().__init__(message=self._message, detail=self.to_response())

    def to_response(self) -> dict:
        payload: dict = {
            "error_code": self._error_code,
            "message":    self._message,
            "service":    self.SERVICE_NAME,
        }
        if self._details is not None:
            payload["details"] = self._details
        return payload


# ──────────────────────────────────────────────────────────────────────────────
# Concrete exceptions
# ──────────────────────────────────────────────────────────────────────────────

class AuthException(BaseAppException):
    """Raised when authentication fails (invalid credentials / bad token)."""

    http_status = 401
    error_code = "AUTH_ERROR"
    log_level = logging.WARNING


class ForbiddenException(BaseAppException):
    """Raised when a token lacks the required scopes."""

    http_status = 403
    error_code = "FORBIDDEN"
    log_level = logging.WARNING


class NotFoundException(BaseAppException):
    """Raised when a requested resource cannot be found."""

    http_status = 404
    error_code = "NOT_FOUND"
    log_level = logging.INFO


class ValidationException(BaseAppException):
    """Raised for business-logic validation failures."""

    http_status = 422
    error_code = "VALIDATION_ERROR"
    log_level = logging.WARNING


class ConflictException(BaseAppException):
    """Raised when the requested action conflicts with the current state."""

    http_status = 409
    error_code = "CONFLICT"
    log_level = logging.WARNING


class ClusterNotFoundException(BaseAppException):
    """Raised when the requested cluster kubeconfig cannot be found."""

    http_status = 404
    error_code = ErrorCode.CLUSTER_NOT_FOUND
    log_level = logging.INFO


class NodeNotFoundException(BaseAppException):
    """Raised when the requested Kubernetes node does not exist."""

    http_status = 404
    error_code = ErrorCode.NODE_NOT_FOUND
    log_level = logging.INFO


class ConfigMapNotFoundException(BaseAppException):
    """Raised when a ConfigMap cannot be read because it does not exist.

    The API server answers 404 for a missing namespace and a missing ConfigMap
    alike, and telling them apart would need read access to namespaces, so the
    message names both and leaves the caller to check which.
    """

    http_status = 404
    error_code = ErrorCode.CONFIGMAP_NOT_FOUND
    log_level = logging.INFO


class NodeNotReadyException(BaseAppException):
    """Raised when uncordon is asked for a node that is not Ready.

    Uncordoning is a promise that the node is fit to take work. Making that
    promise about a node the cluster currently considers unhealthy is almost
    always a mistake — usually someone acting on a stale view of the cluster.

    409, not 400: the request is well-formed and there is no parameter that
    would make it succeed. What has to change is the node, not the call. A 400
    would send the caller looking for a mistake in their own request.

    Note what this does **not** protect against. ``spec.unschedulable`` and the
    Ready condition are independent: an uncordoned NotReady node takes no pods
    *while* it is NotReady, and the scheduler fills it the moment it goes Ready.
    A node that flaps therefore still gets filled during any Ready window, and
    this check — evaluated once, at request time — cannot see that coming. It
    catches operator error, not instability.

    The check is also not atomic with the patch that follows it: a node can go
    NotReady in between, and in a batch the readiness snapshot is taken once up
    front, so the last node of a large batch is judged on a reading several
    round-trips old. Kubernetes offers no compare-and-set on this path, and the
    failure mode is benign — an uncordoned node that has just gone NotReady
    takes no pods until it recovers.
    """

    http_status = 409
    error_code = ErrorCode.NODE_NOT_READY
    log_level = logging.WARNING

    def __init__(self, node_name: str, status: str) -> None:
        super().__init__(
            f"Node '{node_name}' is {status}, so it cannot be uncordoned. "
            f"Uncordoning declares the node fit to receive pods; wait for it "
            f"to report Ready, or investigate why it is not.",
            detail={"node": node_name, "status": status},
        )


class KubeApiException(BaseAppException):
    """Raised when the Kubernetes API returns an unexpected error.

    Wraps ``kubernetes.client.ApiException`` and normalises it into
    the standard error envelope.  HTTP status mirrors the upstream
    K8s status when available, falling back to 502.
    """

    error_code = ErrorCode.KUBE_API_ERROR
    log_level = logging.ERROR

    def __init__(self, message: str, *, kube_status: int | None = None, **kwargs) -> None:
        # Preserve the status the API server actually returned. It can differ
        # from http_status (which floors to 502), and callers rely on the raw
        # value to tell a retryable 503 from a 403. None means no HTTP response
        # at all: the SDK spells that status=0 (TLS failures, a request it
        # could not build), and a bare ApiException() leaves it None. Both are
        # normalised to None rather than invented, since no API server said
        # anything — and NodeService treats a status-less failure as
        # cluster-level.
        self.kube_status = kube_status or None
        # Use the Kubernetes API status as our HTTP status when it makes sense;
        # otherwise default to 502 (bad gateway from the K8s control plane).
        self.http_status = kube_status if kube_status is not None and kube_status >= 400 else 502
        super().__init__(message, **kwargs)


class DrainBlockedException(BaseAppException):
    """Raised when a drain would touch pods that its options do not permit.

    Mirrors ``kubectl drain``'s refusal semantics: a pod with no controller, or
    one using an emptyDir volume, is protected unless the caller explicitly
    opts out. The check runs **before any pod is evicted**, so a rejected drain
    changes nothing on the cluster and the caller learns every blocker in one
    round-trip rather than discovering them one eviction at a time.

    The node is left cordoned — cordoning is what a drain is for, is harmless
    on its own, and re-running with the right flags then has nothing to redo.
    """

    http_status = 400
    error_code = ErrorCode.DRAIN_BLOCKED
    log_level = logging.WARNING

    #: Reason → the option that unblocks it. Order fixes the message ordering.
    UNBLOCKING_OPTION = {
        "unmanaged": "force",
        "emptydir": "delete_emptydir_data",
    }

    def __init__(self, node_name: str, blocked: list[dict]) -> None:
        """``blocked`` — one entry per pod: ``{namespace, name, reasons: [...]}``."""
        required = self._required_options(blocked)

        message = (
            f"Drain of node '{node_name}' is blocked by {len(blocked)} pod(s) "
            f"that the supplied options do not permit removing. No pod was "
            f"evicted and the node is unchanged. Retry with: "
            + ", ".join(f"{k}={v}" for k, v in required.items())
            + "."
        )

        super().__init__(
            message,
            detail={
                "node": node_name,
                "blocked_pods": blocked,
                "required_options": required,
            },
        )

    @classmethod
    def _required_options(cls, blocked: list[dict]) -> dict:
        """Every option needed to unblock the whole set, not just the first pod.

        A caller that fixes one blocker at a time pays a round-trip per blocker,
        which is the slow discovery this endpoint exists to avoid.
        """
        reasons = {r for pod in blocked for r in pod.get("reasons", [])}
        return {
            option: True
            for reason, option in cls.UNBLOCKING_OPTION.items()
            if reason in reasons
        }


# ──────────────────────────────────────────────────────────────────────────────
# Global exception handlers
# ──────────────────────────────────────────────────────────────────────────────

_logger = logging.getLogger(__name__)


def _error_body(code: str, message: str, request_id: str, detail: Any = None) -> dict:
    """Build the standard error response body."""
    error: dict = {"code": code, "message": message}
    if detail is not None:
        error["detail"] = detail
    return {"error": error, "request_id": request_id}


async def app_exception_handler(
    request: Request, exc: BaseAppException
) -> JSONResponse:
    """Handle all BaseAppException subclasses with a unified JSON error response.
    
    If the exception has a 'detail' field (like DeployServiceError), it is 
    forwarded verbatim to the caller.
    """
    request_id: str = getattr(request.state, "request_id", "")
    exc.log(_logger)

    return JSONResponse(
        status_code=exc.http_status,
        content=_error_body(
            code=exc.error_code,
            message=exc.message,
            request_id=request_id,
            detail=exc.detail,
        ),
    )


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Catch-all handler for unexpected exceptions."""
    request_id: str = getattr(request.state, "request_id", "")
    _logger.exception(
        "Unhandled exception | request_id=%s | path=%s",
        request_id,
        request.url.path,
    )

    return JSONResponse(
        status_code=500,
        content=_error_body(
            code="INTERNAL_SERVER_ERROR",
            message="An unexpected error occurred. Please try again later.",
            request_id=request_id,
        ),
    )
