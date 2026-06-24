# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Role

`cluster-service` is one of two FastAPI sub-projects under `antigravity-fastapi/` (the other is `deploy-service/`). This service has three responsibilities:

1. **Kubernetes cluster operations** — list clusters, list/get nodes, cordon, uncordon, drain, label, annotate. Talks directly to multiple Kubernetes clusters via the `kubernetes` SDK.
2. **Deploy-service proxy** — trigger / cancel / retry / status GitLab pipelines by forwarding to `deploy-service` over HTTP with managed bearer-token auth.
3. **Command-execution proxy** — list available commands, run them, poll results, view live logs, and kill running commands by forwarding to `deploy-service`'s SSH command API over HTTP. The upstream identity (`cluster_proxy`) is restricted by deploy-service's per-user whitelist to **ansible commands only**.

The parent repo's `../CLAUDE.md` describes `deploy-service`; this file is for `cluster-service` only. Run all commands from `cluster-service/`.

## Commands

The project uses [uv](https://docs.astral.sh/uv/) for dependency and venv management.

```bash
# Install dependencies (including dev tools)
uv sync --group dev

# Start dev server (hot-reload, loads .env + .env.dev)
APP_ENV=dev uv run uvicorn app.main:app --reload --port 8000

# Run all tests
APP_ENV=test uv run pytest tests/ -v

# Single test file / single test
APP_ENV=test uv run pytest tests/unit/test_node_service.py -v
APP_ENV=test uv run pytest tests/unit/test_node_service.py::test_foo -v

# Coverage
APP_ENV=test uv run pytest tests/ -v --cov=app --cov-report=term-missing

# Generate a bcrypt password hash for data/users.json
make hash p=<password>
```

Makefile shortcuts: `make install`, `make dev`, `make prod`, `make start`, `make test`, `make test-unit`, `make test-int`, `make test-cov`.

## Architecture

Strict layered architecture with Dependency Inversion:

```
router (app/api/v1/)
  └─ service (app/services/)
        └─ repository interface (app/repositories/*_repository.py — ABC)
              └─ concrete impl (Yaml…, Json…)
```

For Kubernetes endpoints there is an additional indirection:

```
router → ClusterRepository.get_kube_client_config(cluster)
       → KubeClientFactory.get_core_v1(cfg)
       → NodeService / ClusterManager (consume the live CoreV1Api)
```

### Key design points

**Config / environments** (`app/core/config.py`): `APP_ENV` selects the env file. Settings loads `.env` then `.env.{APP_ENV}` (override order). `get_settings()` is `lru_cache`'d — reset it in tests with `get_settings.cache_clear()`. `KUBECONFIG_BASE_PATH`, `CORDON_LABEL_REASON`, `CORDON_LABEL_BY`, and the `DEPLOY_SERVICE_*` values are all sourced from here, never hardcoded.

**Auth** (`app/core/security.py`, `app/core/dependencies.py`): JWT (HS256) + bcrypt. Use `Depends(get_current_user(["scope_name"]))` on any route. Three scopes are in use:
- `cluster_api` — gates all `/api/v1/clusters/...` and `/api/v1/clusters/{cluster}/nodes/...` endpoints.
- `deploy_api` — gates all `/api/v1/deploy/...` endpoints.
- `command_api` — gates all `/api/v1/command/...` endpoints.

The `/token` OAuth2 endpoint is registered directly on the root app (not on a versioned router) so Swagger UI can auto-fill Authorization headers.

**Cluster repository abstraction** (`app/repositories/cluster_repository.py`): `ClusterRepository` is an ABC with two concrete impls. Both read from `KUBECONFIG_BASE_PATH`:
- `YamlClusterRepository` — `<cluster>.yaml` files (standard kubeconfig). Cluster name = filename stem.
- `JsonClusterRepository` — `<cluster>.json` files with `{cluster_name, server, ca (base64 PEM), token}`. For service-account-style credentials when no full kubeconfig is available.

Both produce a unified `KubeClientConfig` (`app/domain/kubernetes_models.py`) which `KubeClientFactory` consumes. The factory builds a **fresh** `ApiClient` + `Configuration` per call to prevent cross-cluster state pollution under concurrency — do not cache or reuse `CoreV1Api` across requests.

**Node operations** (`app/services/node_service.py`):
- `cordon` patches `spec.unschedulable=true` **and** stamps two labels (`cordon_reason`, `cordon_by`) whose *values* come from `Settings.CORDON_LABEL_REASON` / `CORDON_LABEL_BY`. `uncordon` removes them.
- `drain` always skips DaemonSet pods, mirror/static pods, and completed/failed pods (not user-configurable). Eviction honours PDBs by default; pass `disable_eviction=true` in `DrainOptions` to bypass with a raw delete. `dry_run` is resolved at the router layer and never reaches the service.
- `label_node` / `annotate_node` accept a `set` map and a `remove` list, then re-read the node and return the full current label/annotation state in the response.

**Deploy-service client** (`app/clients/deploy_service_client.py` + `app/core/token_manager.py`):
- `DeployServiceTokenManager` (subclass of abstract `TokenManager`) fetches and caches a bearer token from deploy-service's `/token` endpoint. Refresh is `asyncio.Lock`-guarded and triggers automatically 30s before expiry. There is a **module-level singleton** in `app/api/v1/deploy.py` (`_deploy_token_manager`) — do not instantiate a second one per request.
- `DeployServiceClient._request_with_retry` retries once on 401 after forcing a token refresh, then maps any non-2xx to `DeployServiceError`.
- `PipelineService` is a thin orchestration layer so the router stays HTTP-only and the client is easy to mock in tests.

**Command-service client** (`app/clients/command_service_client.py` + `app/services/command_service.py`): same pattern as the pipeline proxy — reuses the shared `DeployServiceTokenManager` singleton (upstream identity `cluster_proxy`). The HTML log viewer (`/execution/{id}/view`) is served locally and polls cluster-service's own `/trace/ui`, so browsers never reach deploy-service. `/view` is unauthed; `/trace/ui` uses cookie-or-header auth.

**Exception hierarchy** (`app/core/exceptions.py`): All app exceptions extend `BaseAppException` (carries `http_status`, `error_code`, `log_level`, auto-detected `source_function`). The global handler in `main.py` returns `{"error": {"code": "...", "message": "..."}, "request_id": "..."}`. Notable specialisations:
- `KubeApiException` mirrors the upstream Kubernetes status into `http_status` (falls back to 502). All `kubernetes.client.ApiException`s are caught **inside services** and re-raised as this — the router layer never sees the K8s SDK.
- `DeployServiceError` (extends `UpstreamServiceException`, http 502) adapts deploy-service's error body via `_DEPLOY_CODE_MAP` (body `error.code`) with `_DEPLOY_STATUS_MAP` (HTTP status) as fallback. Update both maps together when adding a new upstream code.
- `ErrorCode` (`StrEnum`) holds all business-level codes — keep new codes there, do not hardcode strings.

**Response envelope**: Success → `ApiResponse[T]` → `{"data": <T>, "request_id": "..."}`. Errors → `{"error": {"code", "message", "detail?"}, "request_id"}`. The `request_id` is propagated from the `X-Coordination-ID` header via `RequestIdMiddleware` and echoed in the response header.

**App factory** (`app/main.py`): `create_app()` returns the FastAPI instance; the module-level `app = create_app()` line is what uvicorn targets. Swagger UI / ReDoc routes are only registered when `DEBUG=true` and serve from `app/static/docs-assets/` for offline use.

### Adding a new protected endpoint

1. Create a router in `app/api/v1/`.
2. Annotate route deps with `Depends(get_current_user(["cluster_api"]))` (or the correct scope).
3. For Kubernetes endpoints: depend on `_get_cluster_repo` → `repo.get_kube_client_config(cluster)` → `KubeClientFactory().get_core_v1(cfg)`, then pass the `CoreV1Api` into the service. **Do not import the `kubernetes` SDK from a router.**
4. Mount the router in `app/api/router.py`.
5. Add the scope to the relevant entries in `data/users.json`.

## Environment Files

| File | Purpose |
|------|---------|
| `.env` | Base values (always loaded) |
| `.env.dev` | Dev overrides (`DEBUG=true`, etc.) |
| `.env.prod` | Prod overrides (replace `SECRET_KEY`) |
| `.env.test` | Test overrides (short token TTL, fixture user path) |

## Tests

- `tests/conftest.py` sets `APP_ENV=test` before any app import and provides a session-scoped `TestClient`.
- `tests/fixtures/users.json` is the user file loaded in test mode.
- Unit tests mock repository / client dependencies directly; integration tests use the full `TestClient`.
- `asyncio_mode = "auto"` is set in `pyproject.toml` — no `@pytest.mark.asyncio` needed.
- The `kubernetes` SDK is **not** mocked at the SDK level — tests inject a fake `CoreV1Api`-shaped object into `NodeService`. Follow that pattern rather than patching `kubernetes.client`.

## Other directories

- `rest_client/` — `.http` files (`auth.http`, `cluster.http`, `deploy.http`) for manual API exploration in JetBrains / VS Code REST Client.
- `data/users.json` — accounts, bcrypt hashes, scopes. Use `make hash p=<password>` or `POST /api/v1/auth/hash-password` to generate hashes.
- `data/kubeconfigs/` — default `KUBECONFIG_BASE_PATH`; drop `<cluster>.yaml` or `<cluster>.json` files here for the cluster repositories to discover.
