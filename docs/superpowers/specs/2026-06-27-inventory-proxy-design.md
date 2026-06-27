# Inventory Proxy API — Design

**Date:** 2026-06-27
**Service:** cluster-service
**Status:** Approved

## Goal

Expose four inventory endpoints on cluster-service, gated by a new
`inventory_api` scope, that proxy to deploy-service's existing
`/api/v1/inventory/*` API. Resolution logic (node_type→bastion_type, slash
detection, regex matching) stays entirely in deploy-service; cluster-service is
a thin proxy — mirroring the existing deploy/command proxy pattern.

### Endpoints

| cluster-service | proxied deploy-service endpoint | scope |
|---|---|---|
| `GET /api/v1/inventory/nodes/{node_name}` | same | `inventory_api` |
| `GET /api/v1/inventory/mappings?type=` | same | `inventory_api` |
| `GET /api/v1/inventory/nodes/{node_name}/bastion-resolution?bastion_type=` | same | `inventory_api` |
| `GET /api/v1/inventory/cluster/bastion-resolution?cluster_name=` | same | `inventory_api` |

## Why proxy (not direct)

cluster-service's defined role is already "deploy-service proxy" + "command
proxy". Adding an inventory proxy keeps a single source of truth for resolution
rules (no config drift between two services), maximal consistency with existing
code, and the smallest change. deploy-service's inventory endpoints are already
`command_api`-gated, and the `cluster_proxy` upstream identity already holds
`command_api` — so **no change to deploy-service is required** for the proxy path.

The only deploy-service change is the unrelated `ClusterRef.context` field
(below), which the inventory API now returns.

## Architecture

```
router (app/api/v1/inventory.py)
  → InventoryProxyService (app/services/inventory_service.py)
      → DeployServiceClient (app/clients/deploy_service_client.py)
          → deploy-service /api/v1/inventory/*
```

Reuses the module-level `DeployServiceTokenManager` singleton
(`get_deploy_token_manager` in `app/api/v1/deploy.py`) — do not instantiate a
second one.

## Files

### New

**`app/domain/inventory_models.py`** — mirrors deploy-service's inventory
response models so cluster-service never imports deploy-service source (same
convention as `pipeline_models.py`):

- `ClusterRef` — `id: str`, `name: str`, `context: str = ""` (see below)
- `NodeInfo` — `id: str`, `name: str`, `labels: Dict[str, str] = {}` with a
  `null → {}` before-validator (mirror deploy-service `NodeInfo`)
- `ClusterNodeInfo` — `node_type`, `node: NodeInfo`, `cluster: ClusterRef`
- `BastionMapping` — `patterns: List[str]`, `runner`, `bastion`, `bastion_ip`
- `NodeBastionResolution` — `node_type`, `node`, `cluster`, `bastion_type`,
  `bastion_type_source: Literal["config","query_param"]`, `matched_mapping`,
  `matched_pattern`
- `ClusterBastionResolution` — `cluster_name`, `has_slash`, `bastion_type`,
  `matched_mapping`, `matched_pattern`

**`app/services/inventory_service.py`** — `InventoryProxyService`, thin
orchestration over `DeployServiceClient` (constructor-injected for testability,
same pattern as `PipelineService`). Four methods:
`get_node`, `list_mappings`, `resolve_node_bastion`, `resolve_cluster_bastion`.
Each calls the client and returns the corresponding domain model. This layer
also performs the 404 unwrap (below).

**`app/api/v1/inventory.py`** — router, prefix `/inventory`, tag `inventory`.
Each route: `Depends(get_current_user(["inventory_api"]))`, returns
`ApiResponse[T]` with `request_id` from `request.state`. A
`_get_inventory_service` dependency builds the service from a
`DeployServiceClient` backed by the shared token-manager singleton (mirror
`deploy.py::_get_pipeline_service`).

### Modified

**`app/clients/deploy_service_client.py`** — add four methods using the existing
`_request_with_retry` (one-time 401 retry, maps non-2xx → `DeployServiceError`):

- `get_node(node_name) -> ClusterNodeInfo`
- `list_mappings(type_name) -> list[BastionMapping]`
- `resolve_node_bastion(node_name, bastion_type=None) -> NodeBastionResolution`
- `resolve_cluster_bastion(cluster_name) -> ClusterBastionResolution`

Each unwraps the `{"data": ...}` envelope and validates into the domain model.

**`app/api/router.py`** — `include_router(inventory_router)`; update the route
list docstring at the top.

**`data/users.json`** — add `inventory_api` to `admin` only.

**`tests/fixtures/users.json`** — add `inventory_api` to `test_admin` so
integration tests can exercise the scope.

### `ClusterRef.context` field (both services)

The inventory API's node-cluster-lookup response now includes
`cluster.context` (fake-api `data/cluster-node-lookup.json` already returns
`"context": "c1"`). It may be absent or empty.

- **deploy-service** `app/repositories/inventory_repository.py` `ClusterRef`:
  add `context: str = ""` plus a `field_validator("context", mode="before")`
  coercing `None → ""` (mirror the existing `NodeInfo._coerce_null_labels`).
  All existing `ClusterRef(id=..., name=...)` constructions in tests stay valid
  thanks to the default.
- **cluster-service** `app/domain/inventory_models.py` `ClusterRef`: same
  field + validator, mirrored.

## Error handling

The client maps any upstream non-2xx → `DeployServiceError` (HTTP 502) via
`_request_with_retry`. For inventory, an upstream **404** (node/mapping not
found) should surface to the cluster-service caller as a **404**, not 502.

- Add `ErrorCode.INVENTORY_NOT_FOUND = "INVENTORY_NOT_FOUND"`.
- In `InventoryProxyService`, wrap each client call: catch `DeployServiceError`,
  and if `exc.upstream_status == 404` (the field `DeployServiceError` stores the
  upstream HTTP status under — **not** `http_status`), raise cluster-service's
  `NotFoundException` (`error_code` overridden to `INVENTORY_NOT_FOUND`) with a
  clear message; otherwise re-raise unchanged (still 502).
- This unwrap lives only in the inventory service — it does not touch the shared
  `_DEPLOY_CODE_MAP` / `_DEPLOY_STATUS_MAP` used by the deploy/command proxies.

## Testing

Mirrors existing deploy-proxy test style.

- **Unit** (`tests/unit/test_inventory_proxy_service.py`): inject a mock
  `DeployServiceClient`; verify each of the four methods forwards correctly and
  returns the right model. Cover `context` present, `context == ""`, and
  `context` absent. Cover the 404 unwrap (mock client raises
  `DeployServiceError(http_status=404)` → service raises `NotFoundException`);
  cover a 502 passing through unchanged.
- **Integration** (`tests/integration/test_inventory_routes.py`): `TestClient`
  with a mocked `DeployServiceClient` (patch the client or its
  `_request_with_retry`). Verify for all four routes: scope gating (no
  `inventory_api` → 403), path/query forwarding, response envelope shape,
  and that an upstream 404 yields HTTP 404.

## Out of scope

- No direct Inventory API connection from cluster-service (no
  `INVENTORY_API_*`, `BASTION_NODE_TYPE_MAP`, `CLUSTER_SLASH_TYPE_MAP` config).
- No changes to deploy-service inventory routing/scopes.
- No new operator/inventory accounts — `inventory_api` goes to `admin` only.
