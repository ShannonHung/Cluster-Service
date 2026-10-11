"""
tests/unit/test_kube_client.py

Per-request Kubernetes clients must not leak.

The factory builds a fresh client per request on purpose — reuse across
requests risks cross-cluster contamination under concurrency — so whatever a
client holds has to be released when its request is done:

- **sockets.** ``ApiClient.close()`` only shuts a thread pool that synchronous
  calls never create; the connections live in ``rest_client.pool_manager``,
  which must be cleared explicitly.
- **CA files.** The SDK accepts a CA only as a file path. Writing one per
  request and never deleting it grows the temp directory without bound; the
  same CA is written once and reused instead.

No network: a client opens no connection until its first request.
"""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from kubernetes.client import ApiClient, Configuration, CoreV1Api

from app.domain.kubernetes_models import KubeClientConfig
from app.services import kube_client
from app.services.dry_run_kube_client import DryRunCoreV1Api
from app.services.kube_client import KubeClientFactory, call_kube


class _Repo:
    def get_kube_client_config(self, cluster):
        return KubeClientConfig(cluster_name=cluster, source="json", server="https://x", token="t")


def _real_client() -> CoreV1Api:
    return CoreV1Api(api_client=ApiClient(configuration=Configuration()))


@pytest.fixture
def built(monkeypatch) -> list[CoreV1Api]:
    """Every client call_kube builds, its connection pool's clear() spied on.

    Spying on each client's own pool — reached through the same
    api_client.rest_client.pool_manager chain release uses — means a test fails
    if release stops clearing it, and also if an SDK upgrade renames any link
    in that chain.
    """
    clients: list[CoreV1Api] = []

    def get_core_v1(self, cfg):
        kube = _real_client()
        pool = kube.api_client.rest_client.pool_manager
        pool.clear = MagicMock(wraps=pool.clear)
        clients.append(kube)
        return kube

    monkeypatch.setattr(KubeClientFactory, "get_core_v1", get_core_v1)
    return clients


def _pool_clear(kube: CoreV1Api) -> MagicMock:
    return kube.api_client.rest_client.pool_manager.clear


# ── sockets ───────────────────────────────────────────────────────────────────


def _raise(exc: Exception):
    raise exc


async def test_the_connection_pool_is_cleared_after_the_call(built):
    await call_kube(_Repo(), "c1", lambda kube: None)
    _pool_clear(built[0]).assert_called_once()


async def test_the_client_is_released_even_when_the_call_raises(built):
    with pytest.raises(RuntimeError):
        await call_kube(_Repo(), "c1", lambda kube: _raise(RuntimeError("op failed")))
    _pool_clear(built[0]).assert_called_once()


async def test_a_failing_release_does_not_hide_the_result(built, monkeypatch):
    """Releasing is housekeeping; it must never replace what the call returned."""
    monkeypatch.setattr(KubeClientFactory, "_release_pool", staticmethod(lambda _: _raise(OSError("boom"))))
    assert await call_kube(_Repo(), "c1", lambda kube: 42) == 42


async def test_a_failing_release_does_not_hide_the_original_error(built, monkeypatch):
    monkeypatch.setattr(KubeClientFactory, "_release_pool", staticmethod(lambda _: _raise(OSError("boom"))))
    with pytest.raises(RuntimeError, match="op failed"):
        await call_kube(_Repo(), "c1", lambda kube: _raise(RuntimeError("op failed")))


async def test_the_result_is_returned_unchanged(built):
    assert await call_kube(_Repo(), "c1", lambda kube: 42) == 42


async def test_each_call_still_gets_its_own_client(built):
    """Releasing must not turn into reuse: the isolation guarantee stands."""
    await call_kube(_Repo(), "c1", lambda kube: None)
    await call_kube(_Repo(), "c1", lambda kube: None)
    assert len(built) == 2 and built[0] is not built[1]


async def test_a_client_without_a_connection_pool_is_left_alone(monkeypatch):
    """The dry-run fake (and any other stand-in) holds no sockets."""
    monkeypatch.setattr(
        KubeClientFactory, "get_core_v1", lambda self, cfg: DryRunCoreV1Api("c1")
    )
    assert await call_kube(_Repo(), "c1", lambda kube: "ok") == "ok"


# ── CA files ──────────────────────────────────────────────────────────────────

_CA_A = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nA\n").decode()
_CA_B = base64.b64encode(b"-----BEGIN CERTIFICATE-----\nB\n").decode()


def _token_cfg(ca: str) -> KubeClientConfig:
    return KubeClientConfig(
        cluster_name="c1", source="json", server="https://x", token="t", ca_data=ca
    )


@pytest.fixture(autouse=True)
def _isolated_ca_files(monkeypatch):
    """The CA cache is process-global; give each test its own and delete the
    files it wrote, rather than leaving them for atexit."""
    monkeypatch.setattr(kube_client, "_CA_FILES", {})
    yield
    for path in kube_client._CA_FILES.values():
        Path(path).unlink(missing_ok=True)


def _ca_path(ca: str) -> Path:
    api_client = KubeClientFactory()._make_api_client(_token_cfg(ca))
    return Path(api_client.configuration.ssl_ca_cert)


def test_the_same_ca_is_written_once_and_reused():
    first, second = _ca_path(_CA_A), _ca_path(_CA_A)
    assert first == second
    assert first.read_bytes() == base64.b64decode(_CA_A)


def test_a_different_ca_gets_its_own_file():
    assert _ca_path(_CA_A) != _ca_path(_CA_B)
    assert _ca_path(_CA_B).read_bytes() == base64.b64decode(_CA_B)


def test_many_requests_do_not_grow_the_number_of_ca_files():
    paths = {_ca_path(_CA_A) for _ in range(20)}
    assert len(paths) == 1
    assert len(kube_client._CA_FILES) == 1


def test_a_ca_file_deleted_from_disk_is_written_again():
    """A tmp cleaner may remove it while the process runs; the next client must
    not point at a missing file."""
    path = _ca_path(_CA_A)
    path.unlink()
    assert _ca_path(_CA_A).exists()
