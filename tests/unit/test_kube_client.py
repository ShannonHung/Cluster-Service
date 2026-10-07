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
from unittest.mock import patch

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
    """Every client call_kube builds, so a test can inspect it afterwards."""
    clients: list[CoreV1Api] = []

    def get_core_v1(self, cfg):
        clients.append(_real_client())
        return clients[-1]

    monkeypatch.setattr(KubeClientFactory, "get_core_v1", get_core_v1)
    return clients


# ── sockets ───────────────────────────────────────────────────────────────────


async def test_the_connection_pool_is_cleared_after_the_call(built):
    with patch("urllib3.PoolManager.clear") as clear:
        await call_kube(_Repo(), "c1", lambda kube: None)
    clear.assert_called_once()


async def test_the_client_is_released_even_when_the_call_raises(built):
    with patch("urllib3.PoolManager.clear") as clear:
        with pytest.raises(RuntimeError):
            await call_kube(_Repo(), "c1", lambda kube: (_ for _ in ()).throw(RuntimeError()))
    clear.assert_called_once()


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


def _ca_path(ca: str) -> Path:
    api_client = KubeClientFactory().get_api_client(_token_cfg(ca))
    return Path(api_client.configuration.ssl_ca_cert)


def test_the_same_ca_is_written_once_and_reused():
    first, second = _ca_path(_CA_A), _ca_path(_CA_A)
    assert first == second
    assert first.read_bytes() == base64.b64decode(_CA_A)


def test_a_different_ca_gets_its_own_file():
    assert _ca_path(_CA_A) != _ca_path(_CA_B)
    assert _ca_path(_CA_B).read_bytes() == base64.b64decode(_CA_B)


def test_many_requests_do_not_grow_the_number_of_ca_files():
    before = len(kube_client._CA_FILES)
    for _ in range(20):
        _ca_path(_CA_A)
    assert len(kube_client._CA_FILES) <= before + 1


def test_a_ca_file_deleted_from_disk_is_written_again():
    """A tmp cleaner may remove it while the process runs; the next client must
    not point at a missing file."""
    path = _ca_path(_CA_A)
    path.unlink()
    assert _ca_path(_CA_A).exists()
