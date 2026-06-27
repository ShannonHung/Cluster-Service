# Inventory Proxy API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add four `inventory_api`-gated endpoints to cluster-service that proxy to deploy-service's `/api/v1/inventory/*`, plus a `ClusterRef.context` field in both services.

**Architecture:** Thin proxy mirroring the existing deploy/command proxies: `router → InventoryProxyService → DeployServiceClient → deploy-service`. Resolution logic stays in deploy-service. cluster-service never imports deploy-service source — it mirrors the response models in its own `app/domain/inventory_models.py`.

**Tech Stack:** FastAPI, httpx (async), Pydantic v2, pytest (`asyncio_mode=auto`), uv.

## Global Constraints

- Run all commands from `cluster-service/` (the `.git` lives inside this dir).
- Tests run with `APP_ENV=test uv run pytest ...`.
- All four cluster-service endpoints require the `inventory_api` scope; it is granted to `admin` only (and `test_admin` in fixtures).
- Reuse the module-level `DeployServiceTokenManager` singleton via `get_deploy_token_manager` from `app/api/v1/deploy.py`; never instantiate a second token manager.
- An upstream 404 must surface as cluster-service HTTP 404 (`INVENTORY_NOT_FOUND`); any other non-2xx stays HTTP 502 (`DeployServiceError`).
- `DeployServiceError` stores the upstream status under the attribute `upstream_status` (NOT `http_status`).
- cluster-service `ApiResponse[T]` and `User` import from `app.domain.models`.
- Mirror existing patterns: `pipeline_models.py` for domain models, `deploy.py`/`command.py` for routers, `pipeline_service.py` for the thin service, `test_command_routes.py` for integration tests (override the service via `app.dependency_overrides`).
- The deploy-service half (Task 6) is committed in deploy-service's own repo, not cluster-service's. Both repos have `.git` inside their respective sub-dirs.

---

### Task 1: Mirror inventory response models in cluster-service

**Files:**
- Create: `app/domain/inventory_models.py`
- Test: `tests/unit/test_inventory_models.py`

**Interfaces:**
- Produces: `ClusterRef`, `NodeInfo`, `ClusterNodeInfo`, `BastionMapping`, `NodeBastionResolution`, `ClusterBastionResolution` (all `pydantic.BaseModel`). `ClusterRef(id: str, name: str, context: str = "")`. `ClusterNodeInfo(node_type: str, node: NodeInfo, cluster: ClusterRef)`. `BastionMapping(patterns: list[str], runner: str, bastion: str, bastion_ip: str)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_inventory_models.py
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    ClusterRef,
    NodeBastionResolution,
    NodeInfo,
)


def test_cluster_ref_context_defaults_empty():
    ref = ClusterRef(id="1", name="type1-cluster-c1")
    assert ref.context == ""


def test_cluster_ref_context_coerces_null_to_empty():
    ref = ClusterRef.model_validate({"id": "1", "name": "c1", "context": None})
    assert ref.context == ""


def test_cluster_ref_context_passthrough():
    ref = ClusterRef(id="1", name="c1", context="c1")
    assert ref.context == "c1"


def test_node_info_null_labels_coerced():
    n = NodeInfo.model_validate({"id": "1", "name": "node1", "labels": None})
    assert n.labels == {}


def test_cluster_node_info_full():
    info = ClusterNodeInfo.model_validate(
        {
            "node_type": "baremetal",
            "node": {"id": "1", "name": "node1", "labels": {"dc": "1"}},
            "cluster": {"id": "1", "name": "type1-cluster-c1", "context": "c1"},
        }
    )
    assert info.cluster.context == "c1"
    assert info.node.labels["dc"] == "1"


def test_bastion_resolution_models_construct():
    mapping = BastionMapping(
        patterns=["type1-.*"], runner="r1", bastion="bastion-a", bastion_ip="10.0.0.5"
    )
    node_res = NodeBastionResolution(
        node_type="baremetal",
        node=NodeInfo(id="1", name="node1"),
        cluster=ClusterRef(id="1", name="type1-cluster-c1", context="c1"),
        bastion_type="type1",
        bastion_type_source="config",
        matched_mapping=mapping,
        matched_pattern="type1-.*",
    )
    assert node_res.bastion_type_source == "config"
    cluster_res = ClusterBastionResolution(
        cluster_name="type1-cluster-c1",
        has_slash=False,
        bastion_type="type1",
        matched_mapping=mapping,
        matched_pattern="type1-.*",
    )
    assert cluster_res.has_slash is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_inventory_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.domain.inventory_models'`

- [ ] **Step 3: Write the implementation**

```python
# app/domain/inventory_models.py
"""
app/domain/inventory_models.py

Pydantic models mirroring deploy-service's inventory API response shapes.

Mirrors deploy-service's app/repositories/inventory_repository.py so that
cluster-service stays fully independent — it never imports deploy-service's
source tree. Keep these in sync with that module.
"""

from __future__ import annotations

from typing import Dict, List, Literal

from pydantic import BaseModel, Field, field_validator


class ClusterRef(BaseModel):
    id: str
    name: str
    # The inventory API may omit context or return null; tolerate both.
    context: str = ""

    @field_validator("context", mode="before")
    @classmethod
    def _coerce_null_context(cls, v: object) -> object:
        return v if v is not None else ""


class NodeInfo(BaseModel):
    id: str
    name: str
    labels: Dict[str, str] = Field(default_factory=dict)

    @field_validator("labels", mode="before")
    @classmethod
    def _coerce_null_labels(cls, v: object) -> object:
        return v if v is not None else {}


class ClusterNodeInfo(BaseModel):
    node_type: str
    node: NodeInfo
    cluster: ClusterRef


class BastionMapping(BaseModel):
    patterns: List[str]
    runner: str
    bastion: str
    bastion_ip: str


class NodeBastionResolution(BaseModel):
    node_type: str
    node: NodeInfo
    cluster: ClusterRef
    bastion_type: str
    bastion_type_source: Literal["config", "query_param"]
    matched_mapping: BastionMapping
    matched_pattern: str


class ClusterBastionResolution(BaseModel):
    cluster_name: str
    has_slash: bool
    bastion_type: str
    matched_mapping: BastionMapping
    matched_pattern: str
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_inventory_models.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add app/domain/inventory_models.py tests/unit/test_inventory_models.py
git commit -m "feat(inventory): mirror inventory response models with ClusterRef.context"
```

---

### Task 2: Add `INVENTORY_NOT_FOUND` error code

**Files:**
- Modify: `app/core/exceptions.py` (the `ErrorCode` enum — add one member)
- Test: `tests/unit/test_inventory_error_code.py`

**Interfaces:**
- Produces: `ErrorCode.INVENTORY_NOT_FOUND == "INVENTORY_NOT_FOUND"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_inventory_error_code.py
from app.core.exceptions import ErrorCode


def test_inventory_not_found_code_exists():
    assert ErrorCode.INVENTORY_NOT_FOUND == "INVENTORY_NOT_FOUND"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_inventory_error_code.py -v`
Expected: FAIL — `AttributeError: INVENTORY_NOT_FOUND`

- [ ] **Step 3: Add the enum member**

In `app/core/exceptions.py`, inside `class ErrorCode(StrEnum)`, add a new section just before the `# ── Generic upstream ──` comment:

```python
    # ── Inventory ─────────────────────────────────────────────────────────────
    INVENTORY_NOT_FOUND        = "INVENTORY_NOT_FOUND"

```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_inventory_error_code.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/core/exceptions.py tests/unit/test_inventory_error_code.py
git commit -m "feat(inventory): add INVENTORY_NOT_FOUND error code"
```

---

### Task 3: Add inventory methods to DeployServiceClient

**Files:**
- Modify: `app/clients/deploy_service_client.py`
- Test: `tests/unit/test_deploy_client_inventory.py`

**Interfaces:**
- Consumes: `DeployServiceClient(base_url, token_manager, timeout=30.0)`, its `_request_with_retry(method, path, context, **kwargs) -> dict` (returns the parsed JSON body, raises `DeployServiceError` on non-2xx), and the Task 1 models.
- Produces (new methods on `DeployServiceClient`):
  - `async get_node(node_name: str) -> ClusterNodeInfo`
  - `async list_mappings(type_name: str) -> list[BastionMapping]`
  - `async resolve_node_bastion(node_name: str, bastion_type: str | None = None) -> NodeBastionResolution`
  - `async resolve_cluster_bastion(cluster_name: str) -> ClusterBastionResolution`

Note: `DeployServiceClient._request_with_retry` builds its own `httpx.AsyncClient` and has no `_transport` slot. To test transport-level behaviour without a live server, these unit tests inject a fake via a small stub on `_request_with_retry` (monkeypatch), matching how thin client methods are unit-tested elsewhere.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_deploy_client_inventory.py
from unittest.mock import AsyncMock

import pytest

from app.clients.deploy_service_client import DeployServiceClient
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    NodeBastionResolution,
)


def _client() -> DeployServiceClient:
    tm = AsyncMock()
    tm.get_token = AsyncMock(return_value="t")
    return DeployServiceClient(base_url="http://deploy", token_manager=tm)


async def test_get_node_unwraps_data(monkeypatch):
    c = _client()
    payload = {
        "data": {
            "node_type": "baremetal",
            "node": {"id": "1", "name": "node1", "labels": {}},
            "cluster": {"id": "1", "name": "type1-cluster-c1", "context": "c1"},
        },
        "request_id": "r1",
    }
    captured = {}

    async def fake_req(method, path, context, **kwargs):
        captured.update(method=method, path=path, kwargs=kwargs)
        return payload

    monkeypatch.setattr(c, "_request_with_retry", fake_req)
    result = await c.get_node("node1")
    assert isinstance(result, ClusterNodeInfo)
    assert result.cluster.context == "c1"
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/inventory/nodes/node1"


async def test_list_mappings_unwraps_list(monkeypatch):
    c = _client()
    payload = {
        "data": [
            {"patterns": ["type1-.*"], "runner": "r", "bastion": "b", "bastion_ip": "10.0.0.5"}
        ],
        "request_id": "r1",
    }
    captured = {}

    async def fake_req(method, path, context, **kwargs):
        captured.update(path=path, kwargs=kwargs)
        return payload

    monkeypatch.setattr(c, "_request_with_retry", fake_req)
    result = await c.list_mappings("type1")
    assert len(result) == 1
    assert isinstance(result[0], BastionMapping)
    assert captured["path"] == "/api/v1/inventory/mappings"
    assert captured["kwargs"]["params"] == {"type": "type1"}


async def test_resolve_node_bastion_forwards_override(monkeypatch):
    c = _client()
    payload = {
        "data": {
            "node_type": "baremetal",
            "node": {"id": "1", "name": "node1", "labels": {}},
            "cluster": {"id": "1", "name": "type1-cluster-c1", "context": "c1"},
            "bastion_type": "type1",
            "bastion_type_source": "query_param",
            "matched_mapping": {
                "patterns": ["type1-.*"], "runner": "r", "bastion": "b", "bastion_ip": "10.0.0.5"
            },
            "matched_pattern": "type1-.*",
        },
        "request_id": "r1",
    }
    captured = {}

    async def fake_req(method, path, context, **kwargs):
        captured.update(path=path, kwargs=kwargs)
        return payload

    monkeypatch.setattr(c, "_request_with_retry", fake_req)
    result = await c.resolve_node_bastion("node1", bastion_type="type1")
    assert isinstance(result, NodeBastionResolution)
    assert captured["path"] == "/api/v1/inventory/nodes/node1/bastion-resolution"
    assert captured["kwargs"]["params"] == {"bastion_type": "type1"}


async def test_resolve_node_bastion_omits_none_override(monkeypatch):
    c = _client()
    payload = {
        "data": {
            "node_type": "baremetal",
            "node": {"id": "1", "name": "node1", "labels": {}},
            "cluster": {"id": "1", "name": "type1-cluster-c1", "context": "c1"},
            "bastion_type": "type1",
            "bastion_type_source": "config",
            "matched_mapping": {
                "patterns": ["type1-.*"], "runner": "r", "bastion": "b", "bastion_ip": "10.0.0.5"
            },
            "matched_pattern": "type1-.*",
        },
        "request_id": "r1",
    }
    captured = {}

    async def fake_req(method, path, context, **kwargs):
        captured.update(kwargs=kwargs)
        return payload

    monkeypatch.setattr(c, "_request_with_retry", fake_req)
    await c.resolve_node_bastion("node1")
    assert captured["kwargs"].get("params") == {}


async def test_resolve_cluster_bastion(monkeypatch):
    c = _client()
    payload = {
        "data": {
            "cluster_name": "type1-cluster-c1",
            "has_slash": False,
            "bastion_type": "type1",
            "matched_mapping": {
                "patterns": ["type1-.*"], "runner": "r", "bastion": "b", "bastion_ip": "10.0.0.5"
            },
            "matched_pattern": "type1-.*",
        },
        "request_id": "r1",
    }
    captured = {}

    async def fake_req(method, path, context, **kwargs):
        captured.update(path=path, kwargs=kwargs)
        return payload

    monkeypatch.setattr(c, "_request_with_retry", fake_req)
    result = await c.resolve_cluster_bastion("type1-cluster-c1")
    assert isinstance(result, ClusterBastionResolution)
    assert captured["path"] == "/api/v1/inventory/cluster/bastion-resolution"
    assert captured["kwargs"]["params"] == {"cluster_name": "type1-cluster-c1"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_deploy_client_inventory.py -v`
Expected: FAIL — `AttributeError: 'DeployServiceClient' object has no attribute 'get_node'`

- [ ] **Step 3: Add the import and the four methods**

In `app/clients/deploy_service_client.py`, add to the imports block (after the existing `from app.domain.pipeline_models import ...`):

```python
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    NodeBastionResolution,
)
```

Then append these methods to the `DeployServiceClient` class (after `retry_pipeline`):

```python
    # ── inventory proxy ───────────────────────────────────────────────────────

    async def get_node(self, node_name: str) -> ClusterNodeInfo:
        """Look up cluster node info by node name."""
        raw = await self._request_with_retry(
            "GET",
            f"/api/v1/inventory/nodes/{node_name}",
            context="inventory.get_node",
        )
        return ClusterNodeInfo(**raw["data"])

    async def list_mappings(self, type_name: str) -> list[BastionMapping]:
        """List bastion-cluster mappings for a bastion type."""
        raw = await self._request_with_retry(
            "GET",
            "/api/v1/inventory/mappings",
            context="inventory.list_mappings",
            params={"type": type_name},
        )
        return [BastionMapping(**item) for item in raw["data"]]

    async def resolve_node_bastion(
        self, node_name: str, bastion_type: str | None = None
    ) -> NodeBastionResolution:
        """Resolve a node name to its bastion runner."""
        params: dict[str, str] = {}
        if bastion_type is not None:
            params["bastion_type"] = bastion_type
        raw = await self._request_with_retry(
            "GET",
            f"/api/v1/inventory/nodes/{node_name}/bastion-resolution",
            context="inventory.resolve_node_bastion",
            params=params,
        )
        return NodeBastionResolution(**raw["data"])

    async def resolve_cluster_bastion(
        self, cluster_name: str
    ) -> ClusterBastionResolution:
        """Resolve a cluster name to its bastion runner."""
        raw = await self._request_with_retry(
            "GET",
            "/api/v1/inventory/cluster/bastion-resolution",
            context="inventory.resolve_cluster_bastion",
            params={"cluster_name": cluster_name},
        )
        return ClusterBastionResolution(**raw["data"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_deploy_client_inventory.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/clients/deploy_service_client.py tests/unit/test_deploy_client_inventory.py
git commit -m "feat(inventory): add inventory proxy methods to DeployServiceClient"
```

---

### Task 4: InventoryProxyService with 404 unwrap

**Files:**
- Create: `app/services/inventory_service.py`
- Test: `tests/unit/test_inventory_proxy_service.py`

**Interfaces:**
- Consumes: `DeployServiceClient` (Task 3 methods), `DeployServiceError` (has `.upstream_status: int`), `NotFoundException`, `ErrorCode.INVENTORY_NOT_FOUND` (Task 2), Task 1 models.
- Produces: `InventoryProxyService(client: DeployServiceClient)` with `async get_node`, `async list_mappings`, `async resolve_node_bastion`, `async resolve_cluster_bastion` — same signatures/returns as the client methods. On `DeployServiceError` with `upstream_status == 404` it raises `NotFoundException` (error_code `INVENTORY_NOT_FOUND`); other `DeployServiceError`s propagate unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_inventory_proxy_service.py
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import DeployServiceError, ErrorCode, NotFoundException
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    NodeBastionResolution,
    NodeInfo,
    ClusterRef,
)
from app.services.inventory_service import InventoryProxyService


def _node_info() -> ClusterNodeInfo:
    return ClusterNodeInfo(
        node_type="baremetal",
        node=NodeInfo(id="1", name="node1"),
        cluster=ClusterRef(id="1", name="type1-cluster-c1", context="c1"),
    )


def _mapping() -> BastionMapping:
    return BastionMapping(patterns=["type1-.*"], runner="r", bastion="b", bastion_ip="10.0.0.5")


async def test_get_node_forwards():
    client = AsyncMock()
    client.get_node = AsyncMock(return_value=_node_info())
    svc = InventoryProxyService(client)
    result = await svc.get_node("node1")
    assert result.cluster.context == "c1"
    client.get_node.assert_awaited_once_with("node1")


async def test_list_mappings_forwards():
    client = AsyncMock()
    client.list_mappings = AsyncMock(return_value=[_mapping()])
    svc = InventoryProxyService(client)
    result = await svc.list_mappings("type1")
    assert result[0].bastion_ip == "10.0.0.5"
    client.list_mappings.assert_awaited_once_with("type1")


async def test_resolve_node_bastion_forwards_override():
    client = AsyncMock()
    client.resolve_node_bastion = AsyncMock(
        return_value=NodeBastionResolution(
            node_type="baremetal",
            node=NodeInfo(id="1", name="node1"),
            cluster=ClusterRef(id="1", name="type1-cluster-c1", context="c1"),
            bastion_type="type1",
            bastion_type_source="query_param",
            matched_mapping=_mapping(),
            matched_pattern="type1-.*",
        )
    )
    svc = InventoryProxyService(client)
    result = await svc.resolve_node_bastion("node1", bastion_type="type1")
    assert result.bastion_type_source == "query_param"
    client.resolve_node_bastion.assert_awaited_once_with("node1", bastion_type="type1")


async def test_resolve_cluster_bastion_forwards():
    client = AsyncMock()
    client.resolve_cluster_bastion = AsyncMock(
        return_value=ClusterBastionResolution(
            cluster_name="type1-cluster-c1",
            has_slash=False,
            bastion_type="type1",
            matched_mapping=_mapping(),
            matched_pattern="type1-.*",
        )
    )
    svc = InventoryProxyService(client)
    result = await svc.resolve_cluster_bastion("type1-cluster-c1")
    assert result.has_slash is False
    client.resolve_cluster_bastion.assert_awaited_once_with("type1-cluster-c1")


async def test_upstream_404_becomes_not_found():
    client = AsyncMock()
    client.get_node = AsyncMock(
        side_effect=DeployServiceError(http_status=404, body={"error": {"code": "NOT_FOUND", "message": "no node"}})
    )
    svc = InventoryProxyService(client)
    with pytest.raises(NotFoundException) as exc:
        await svc.get_node("ghost")
    assert exc.value.error_code == ErrorCode.INVENTORY_NOT_FOUND
    assert exc.value.http_status == 404


async def test_upstream_502_propagates():
    client = AsyncMock()
    client.list_mappings = AsyncMock(
        side_effect=DeployServiceError(http_status=503, body={"error": {"code": "X", "message": "down"}})
    )
    svc = InventoryProxyService(client)
    with pytest.raises(DeployServiceError):
        await svc.list_mappings("type1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_inventory_proxy_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.inventory_service'`

- [ ] **Step 3: Write the implementation**

```python
# app/services/inventory_service.py
"""
app/services/inventory_service.py

Thin orchestration over DeployServiceClient for the inventory proxy.

Mirrors PipelineService: the router stays HTTP-only and the client is easy to
mock in tests. The only added logic is translating an upstream 404 into
cluster-service's NotFoundException so callers see a 404 (not the generic 502
that DeployServiceError otherwise yields).
"""

from __future__ import annotations

import logging

from app.clients.deploy_service_client import DeployServiceClient
from app.core.exceptions import DeployServiceError, ErrorCode, NotFoundException
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    NodeBastionResolution,
)

_logger = logging.getLogger(__name__)


class _InventoryNotFound(NotFoundException):
    """NotFoundException specialised with the inventory error code."""

    error_code = ErrorCode.INVENTORY_NOT_FOUND


class InventoryProxyService:
    """Thin proxy over DeployServiceClient's inventory methods."""

    def __init__(self, client: DeployServiceClient) -> None:
        self._client = client

    async def get_node(self, node_name: str) -> ClusterNodeInfo:
        try:
            return await self._client.get_node(node_name)
        except DeployServiceError as exc:
            raise self._maybe_not_found(exc, f"node '{node_name}' not found")

    async def list_mappings(self, type_name: str) -> list[BastionMapping]:
        try:
            return await self._client.list_mappings(type_name)
        except DeployServiceError as exc:
            raise self._maybe_not_found(
                exc, f"no bastion mappings for type '{type_name}'"
            )

    async def resolve_node_bastion(
        self, node_name: str, bastion_type: str | None = None
    ) -> NodeBastionResolution:
        try:
            return await self._client.resolve_node_bastion(
                node_name, bastion_type=bastion_type
            )
        except DeployServiceError as exc:
            raise self._maybe_not_found(
                exc, f"could not resolve bastion for node '{node_name}'"
            )

    async def resolve_cluster_bastion(
        self, cluster_name: str
    ) -> ClusterBastionResolution:
        try:
            return await self._client.resolve_cluster_bastion(cluster_name)
        except DeployServiceError as exc:
            raise self._maybe_not_found(
                exc, f"could not resolve bastion for cluster '{cluster_name}'"
            )

    @staticmethod
    def _maybe_not_found(exc: DeployServiceError, message: str) -> Exception:
        """Return a 404 NotFoundException for upstream 404s; else the original error."""
        if exc.upstream_status == 404:
            return _InventoryNotFound(message)
        return exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_inventory_proxy_service.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add app/services/inventory_service.py tests/unit/test_inventory_proxy_service.py
git commit -m "feat(inventory): add InventoryProxyService with 404 unwrap"
```

---

### Task 5: Inventory router + mount + scope grants

**Files:**
- Create: `app/api/v1/inventory.py`
- Modify: `app/api/router.py`
- Modify: `data/users.json` (grant `inventory_api` to `admin`)
- Modify: `tests/fixtures/users.json` (grant `inventory_api` to `test_admin`)
- Test: `tests/integration/test_inventory_routes.py`

**Interfaces:**
- Consumes: `InventoryProxyService` (Task 4), `DeployServiceClient`, `get_deploy_token_manager` (from `app/api/v1/deploy.py`), `get_current_user` (`app/core/dependencies.py`), `ApiResponse`/`User` (`app/domain/models`), Task 1 models.
- Produces: `router` (APIRouter, prefix `/inventory`) and `_get_inventory_service` dependency (overridable in tests via `app.dependency_overrides`).

- [ ] **Step 1: Write the failing integration test**

```python
# tests/integration/test_inventory_routes.py
"""Integration tests for the inventory proxy router. InventoryProxyService is
overridden with a fake via dependency_overrides so no real deploy-service is
needed."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from app.main import app
from app.api.v1.inventory import _get_inventory_service
from app.core.exceptions import ErrorCode, NotFoundException
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    ClusterRef,
    NodeBastionResolution,
    NodeInfo,
)


def _login(client, username="test_admin", password="secret") -> str:
    r = client.post("/token", data={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _mapping() -> BastionMapping:
    return BastionMapping(patterns=["type1-.*"], runner="r", bastion="b", bastion_ip="10.0.0.5")


@pytest.fixture
def fake_service():
    svc = AsyncMock()
    svc.get_node = AsyncMock(
        return_value=ClusterNodeInfo(
            node_type="baremetal",
            node=NodeInfo(id="1", name="node1"),
            cluster=ClusterRef(id="1", name="type1-cluster-c1", context="c1"),
        )
    )
    svc.list_mappings = AsyncMock(return_value=[_mapping()])
    svc.resolve_node_bastion = AsyncMock(
        return_value=NodeBastionResolution(
            node_type="baremetal",
            node=NodeInfo(id="1", name="node1"),
            cluster=ClusterRef(id="1", name="type1-cluster-c1", context="c1"),
            bastion_type="type1",
            bastion_type_source="config",
            matched_mapping=_mapping(),
            matched_pattern="type1-.*",
        )
    )
    svc.resolve_cluster_bastion = AsyncMock(
        return_value=ClusterBastionResolution(
            cluster_name="type1-cluster-c1",
            has_slash=False,
            bastion_type="type1",
            matched_mapping=_mapping(),
            matched_pattern="type1-.*",
        )
    )
    app.dependency_overrides[_get_inventory_service] = lambda: svc
    yield svc
    app.dependency_overrides.pop(_get_inventory_service, None)


def test_get_node_requires_auth(client):
    r = client.get("/api/v1/inventory/nodes/node1")
    assert r.status_code == 401


def test_get_node_requires_scope(client, fake_service):
    # test_operator has only cluster_api, not inventory_api
    token = _login(client, username="test_operator")
    r = client.get(
        "/api/v1/inventory/nodes/node1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


def test_get_node_ok(client, fake_service):
    token = _login(client)
    r = client.get(
        "/api/v1/inventory/nodes/node1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"]["cluster"]["context"] == "c1"
    assert "request_id" in body
    fake_service.get_node.assert_awaited_once_with("node1")


def test_list_mappings_forwards_type(client, fake_service):
    token = _login(client)
    r = client.get(
        "/api/v1/inventory/mappings",
        params={"type": "type1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"][0]["bastion_ip"] == "10.0.0.5"
    fake_service.list_mappings.assert_awaited_once_with("type1")


def test_node_bastion_resolution_forwards_override(client, fake_service):
    token = _login(client)
    r = client.get(
        "/api/v1/inventory/nodes/node1/bastion-resolution",
        params={"bastion_type": "type1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["bastion_type"] == "type1"
    fake_service.resolve_node_bastion.assert_awaited_once_with("node1", bastion_type="type1")


def test_cluster_bastion_resolution_ok(client, fake_service):
    token = _login(client)
    r = client.get(
        "/api/v1/inventory/cluster/bastion-resolution",
        params={"cluster_name": "type1-cluster-c1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["cluster_name"] == "type1-cluster-c1"
    fake_service.resolve_cluster_bastion.assert_awaited_once_with("type1-cluster-c1")


def test_get_node_404_surfaces(client, fake_service):
    fake_service.get_node = AsyncMock(side_effect=NotFoundException("no node"))
    token = _login(client)
    r = client.get(
        "/api/v1/inventory/nodes/ghost",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 404, r.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `APP_ENV=test uv run pytest tests/integration/test_inventory_routes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api.v1.inventory'`

- [ ] **Step 3: Create the router**

```python
# app/api/v1/inventory.py
"""
app/api/v1/inventory.py

Inventory proxy endpoints — forwarded to deploy-service's /api/v1/inventory/*.
All endpoints require the ``inventory_api`` scope.

  GET /api/v1/inventory/nodes/{node_name}
  GET /api/v1/inventory/mappings?type=
  GET /api/v1/inventory/nodes/{node_name}/bastion-resolution?bastion_type=
  GET /api/v1/inventory/cluster/bastion-resolution?cluster_name=
"""

from __future__ import annotations

import logging
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Query, Request

from app.api.v1.deploy import get_deploy_token_manager
from app.clients.deploy_service_client import DeployServiceClient
from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.core.token_manager import DeployServiceTokenManager
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    NodeBastionResolution,
)
from app.domain.models import ApiResponse, User
from app.services.inventory_service import InventoryProxyService

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inventory", tags=["inventory"])


def _get_inventory_service(
    token_manager: DeployServiceTokenManager = Depends(get_deploy_token_manager),
) -> InventoryProxyService:
    """Build InventoryProxyService backed by a live DeployServiceClient."""
    settings = get_settings()
    client = DeployServiceClient(
        base_url=settings.DEPLOY_SERVICE_URL,
        token_manager=token_manager,
    )
    return InventoryProxyService(client)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


@router.get(
    "/nodes/{node_name}",
    response_model=ApiResponse[ClusterNodeInfo],
    summary="Look up cluster node info by node name",
)
async def get_node(
    request: Request,
    node_name: str,
    svc: InventoryProxyService = Depends(_get_inventory_service),
    current_user: Annotated[User, Depends(get_current_user(["inventory_api"]))] = None,
) -> ApiResponse[ClusterNodeInfo]:
    data = await svc.get_node(node_name)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/mappings",
    response_model=ApiResponse[List[BastionMapping]],
    summary="List bastion-cluster mappings by type",
)
async def get_mappings(
    request: Request,
    type: str = Query(..., description="Bastion type name"),
    svc: InventoryProxyService = Depends(_get_inventory_service),
    current_user: Annotated[User, Depends(get_current_user(["inventory_api"]))] = None,
) -> ApiResponse[List[BastionMapping]]:
    data = await svc.list_mappings(type)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/nodes/{node_name}/bastion-resolution",
    response_model=ApiResponse[NodeBastionResolution],
    summary="Resolve node name to bastion runner",
)
async def get_node_bastion_resolution(
    request: Request,
    node_name: str,
    bastion_type: Optional[str] = Query(
        default=None, description="Override bastion type (default: derived in deploy-service)"
    ),
    svc: InventoryProxyService = Depends(_get_inventory_service),
    current_user: Annotated[User, Depends(get_current_user(["inventory_api"]))] = None,
) -> ApiResponse[NodeBastionResolution]:
    data = await svc.resolve_node_bastion(node_name, bastion_type=bastion_type)
    return ApiResponse(data=data, request_id=_request_id(request))


@router.get(
    "/cluster/bastion-resolution",
    response_model=ApiResponse[ClusterBastionResolution],
    summary="Resolve a cluster name to a bastion runner",
)
async def get_cluster_bastion_resolution(
    request: Request,
    cluster_name: str = Query(..., description="Cluster name to resolve"),
    svc: InventoryProxyService = Depends(_get_inventory_service),
    current_user: Annotated[User, Depends(get_current_user(["inventory_api"]))] = None,
) -> ApiResponse[ClusterBastionResolution]:
    data = await svc.resolve_cluster_bastion(cluster_name)
    return ApiResponse(data=data, request_id=_request_id(request))
```

- [ ] **Step 4: Mount the router**

In `app/api/router.py`, add the import alongside the others:

```python
from app.api.v1.inventory import router as inventory_router
```

Add the include after the command router line:

```python
v1_router.include_router(inventory_router)  # mounts at /api/v1/inventory/...
```

And add to the top-of-file route-layout docstring (after the command rows):

```
  GET  /api/v1/inventory/nodes/{node_name}                    → Cluster node lookup (proxy)
  GET  /api/v1/inventory/mappings                             → Bastion-cluster mappings (proxy)
  GET  /api/v1/inventory/nodes/{node_name}/bastion-resolution → Node-to-bastion resolution (proxy)
  GET  /api/v1/inventory/cluster/bastion-resolution           → Cluster-to-bastion resolution (proxy)
```

- [ ] **Step 5: Grant the scope**

In `data/users.json`, add `"inventory_api"` to the `admin` account's `scopes` list (it currently has `cluster_api`, `deploy_api`, `vm_api`, `command_api`).

In `tests/fixtures/users.json`, add `"inventory_api"` to the `test_admin` account's `scopes` list (it currently has `cluster_api`, `vm_api`, `command_api`). Leave `test_operator` with only `cluster_api` (the 403 test depends on this).

- [ ] **Step 6: Run test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/integration/test_inventory_routes.py -v`
Expected: PASS (8 tests)

- [ ] **Step 7: Commit**

```bash
git add app/api/v1/inventory.py app/api/router.py data/users.json tests/fixtures/users.json tests/integration/test_inventory_routes.py
git commit -m "feat(inventory): add inventory proxy router gated by inventory_api scope"
```

---

### Task 6: Add `ClusterRef.context` to deploy-service (separate repo)

**Files:**
- Modify: `../deploy-service/app/repositories/inventory_repository.py` (the `ClusterRef` model)
- Test: `../deploy-service/tests/unit/test_inventory_repository.py` (add cases) — or a new `test_cluster_ref_context.py` if cleaner.

**Note:** This change is committed in **deploy-service's** git repo (`.git` is inside `deploy-service/`), not cluster-service's. Run these git commands from `../deploy-service/`.

**Interfaces:**
- Produces: `ClusterRef(id: str, name: str, context: str = "")` with a `None → ""` coercion, in deploy-service.

- [ ] **Step 1: Write the failing test**

Add to `../deploy-service/tests/unit/test_inventory_repository.py` (top-level test functions):

```python
def test_cluster_ref_context_defaults_empty():
    from app.repositories.inventory_repository import ClusterRef
    ref = ClusterRef(id="1", name="type1-cluster-c1")
    assert ref.context == ""


def test_cluster_ref_context_coerces_null():
    from app.repositories.inventory_repository import ClusterRef
    ref = ClusterRef.model_validate({"id": "1", "name": "c1", "context": None})
    assert ref.context == ""


def test_cluster_ref_context_passthrough():
    from app.repositories.inventory_repository import ClusterRef
    ref = ClusterRef(id="1", name="c1", context="c1")
    assert ref.context == "c1"
```

- [ ] **Step 2: Run test to verify it fails**

Run (from `../deploy-service/`): `APP_ENV=test uv run pytest tests/unit/test_inventory_repository.py -k context -v`
Expected: FAIL — `ValidationError` / `AttributeError` on `context`

- [ ] **Step 3: Add the field + validator**

In `../deploy-service/app/repositories/inventory_repository.py`, change `ClusterRef` from:

```python
class ClusterRef(BaseModel):
    id: str
    name: str
```

to:

```python
class ClusterRef(BaseModel):
    id: str
    name: str
    # The inventory API may omit context or return null; tolerate both.
    context: str = ""

    @field_validator("context", mode="before")
    @classmethod
    def _coerce_null_context(cls, v: object) -> object:
        return v if v is not None else ""
```

(`field_validator` is already imported in this file.)

- [ ] **Step 4: Run test to verify it passes**

Run (from `../deploy-service/`): `APP_ENV=test uv run pytest tests/unit/test_inventory_repository.py -k context -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full deploy-service inventory suite (regression)**

Run (from `../deploy-service/`): `APP_ENV=test uv run pytest tests/ -k inventory -v`
Expected: PASS — all existing inventory tests still green (existing `ClusterRef(id=..., name=...)` constructions remain valid via the default).

- [ ] **Step 6: Commit (in deploy-service repo)**

```bash
cd ../deploy-service
git checkout -b feat/cluster-ref-context
git add app/repositories/inventory_repository.py tests/unit/test_inventory_repository.py
git commit -m "feat(inventory): add ClusterRef.context field (nullable, defaults empty)"
cd ../cluster-service
```

---

### Task 7: Full regression + spec verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full cluster-service test suite**

Run: `APP_ENV=test uv run pytest tests/ -v`
Expected: PASS — all tests, including pre-existing ones, green. No collection errors.

- [ ] **Step 2: Verify endpoint wiring with a route dump**

Run:
```bash
APP_ENV=test uv run python -c "from app.main import app; print('\n'.join(sorted(r.path for r in app.routes if 'inventory' in getattr(r, 'path', ''))))"
```
Expected output (4 lines):
```
/api/v1/inventory/cluster/bastion-resolution
/api/v1/inventory/mappings
/api/v1/inventory/nodes/{node_name}
/api/v1/inventory/nodes/{node_name}/bastion-resolution
```

- [ ] **Step 3: Run the deploy-service suite (Task 6 regression)**

Run (from `../deploy-service/`): `APP_ENV=test uv run pytest tests/ -m 'not e2e' -v`
Expected: PASS. (CI runs `make test` = `-m 'not e2e'`; keep any new tests non-e2e.)

- [ ] **Step 4: No commit** — verification only. If anything fails, fix in the owning task and re-run.

---

## Notes for the implementer

- **Two repos:** Tasks 1–5 and 7 commit in `cluster-service/` (`.git` inside it). Task 6 commits in `deploy-service/` (`.git` inside it). Don't cross the streams.
- **Branch:** cluster-service work is on `feat/inventory-proxy` (already created from `develop`). Per project workflow, PR into `develop`, never commit to `main`.
- **`get_current_user` scope:** `get_current_user(["inventory_api"])` enforces ALL listed scopes; passing the dependency with `= None` default on the param is the existing idiom (see `deploy.py`).
- **404 mapping caveat:** the 404 unwrap lives only in `InventoryProxyService` (Task 4). Do not edit `_DEPLOY_CODE_MAP` / `_DEPLOY_STATUS_MAP` — those are shared with the deploy/command proxies.
