"""
app/services/kube_errors.py

Kubernetes error translation shared by every service that talks to a cluster.

The SDK never escapes the service layer (CLAUDE.md, "Exception hierarchy"):
every call into ``CoreV1Api`` runs inside ``translate_kube_errors``, the one
place an SDK failure becomes an application exception.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from kubernetes.client.exceptions import ApiException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from app.core.exceptions import BaseAppException, KubeApiException


def connection_error(cluster: str, exc: Urllib3HTTPError) -> KubeApiException:
    """Convert a urllib3 network error to a KubeApiException(503).

    Tagged ``cluster_level`` so batch operations can tell an unreachable
    cluster apart from a per-node failure and propagate it instead of
    reporting it once per node.
    """
    err = KubeApiException(
        f"Cannot reach cluster '{cluster}': {exc}",
        kube_status=503,
    )
    err.cluster_level = True
    return err


@contextmanager
def translate_kube_errors(
    cluster: str,
    what: str,
    *,
    not_found: Callable[[], BaseAppException] | None = None,
) -> Iterator[None]:
    """Translate SDK failures raised inside the block.

    - ``ApiException`` → ``KubeApiException("Failed to <what>: <reason>")``
      carrying the upstream status — or, on a 404, ``not_found()`` when the
      caller names the thing that can be missing (a node, a ConfigMap). A
      listing has nothing to be missing, so it leaves ``not_found`` unset.
    - urllib3 network error → ``connection_error``: a cluster-level 503.
    - anything else, including an application exception the caller raised
      itself inside the block, passes through untouched.

    A caller with a status of its own to handle (drain treats 404 as "already
    gone" and 429 as a PodDisruptionBudget refusal) catches ``ApiException``
    inside the block and re-raises what it does not handle.
    """
    try:
        yield
    except ApiException as exc:
        if exc.status == 404 and not_found is not None:
            raise not_found() from exc
        raise KubeApiException(
            f"Failed to {what}: {exc.reason}",
            kube_status=exc.status,
        ) from exc
    except Urllib3HTTPError as exc:
        raise connection_error(cluster, exc) from exc
