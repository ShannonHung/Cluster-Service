# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Role

`cluster-service` is one of two FastAPI sub-projects under `antigravity-fastapi/` (the other is `deploy-service/`). This service has three responsibilities:

1. **Kubernetes cluster operations** — list clusters, list/get nodes, cordon, uncordon, drain, label, annotate, list pods, list ConfigMaps. Talks directly to multiple Kubernetes clusters via the `kubernetes` SDK.
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

**Auth** (`app/core/security.py`, `app/core/dependencies.py`): JWT (HS256) + bcrypt. Use `Depends(get_current_user(["scope_name"]))` on any route. Four scopes are in use:
- `cluster_api` — gates all `/api/v1/clusters/...` and `/api/v1/clusters/{cluster}/nodes/...` endpoints.
- `deploy_api` — gates all `/api/v1/deploy/...` endpoints.
- `command_api` — gates all `/api/v1/command/...` endpoints.
- `inventory_api` — gates all `/api/v1/inventory/...` endpoints.

The `/token` OAuth2 endpoint is registered directly on the root app (not on a versioned router) so Swagger UI can auto-fill Authorization headers.

**Cluster repository abstraction** (`app/repositories/cluster_repository.py`): `ClusterRepository` is an ABC with two concrete impls. Both read from `KUBECONFIG_BASE_PATH`:
- `YamlClusterRepository` — `<cluster>.yaml` files (standard kubeconfig). Cluster name = filename stem.
- `JsonClusterRepository` — `<cluster>.json` files with `{cluster_name, server, ca (base64 PEM), token}`. For service-account-style credentials when no full kubeconfig is available.

Both produce a unified `KubeClientConfig` (`app/domain/kubernetes_models.py`) which `KubeClientFactory` consumes. The factory builds a **fresh** `ApiClient` + `Configuration` per call to prevent cross-cluster state pollution under concurrency — do not cache or reuse `CoreV1Api` across requests.

**Node operations** (`app/services/node_service.py`):
- `cordon` / `uncordon` patch `spec.unschedulable` and nothing else. Both delegate to the shared `_patch_unschedulable` helper — keep new schedulability operations on that path rather than issuing their own patch.
- **`uncordon` requires the node to be Ready; `cordon` never checks.** Uncordoning declares a node fit for work, so a NotReady or Unknown node is refused with 409 `NODE_NOT_READY` and **no override** — no request parameter can make an unhealthy node healthy. Cordoning is the opposite: it is how an operator responds to a sick node, so it is never gated. The check is an allowlist (only `"Ready"` passes) via `_node_status`, not a list of bad values — a denylist silently admits any status Kubernetes adds later.
- **What the readiness gate is not.** `spec.unschedulable` and the Ready condition are independent: an uncordoned NotReady node takes no pods *while* NotReady, and the scheduler fills it the instant it goes Ready. A flapping node is therefore still filled during its Ready windows. The gate catches action on a stale view of the cluster, not instability — do not describe it as protecting against the latter.
- Batch uncordon resolves readiness with **one `list_node`**, not a read per node, so the gate's cost does not scale with batch size. This adds a **`list` on nodes** RBAC requirement to that endpoint (single-node uncordon uses `read_node` and is unaffected) — a token holding only `patch` fails the batch wholesale, since 403 is cluster-level. Check `JsonClusterRepository` service-account tokens carry it. A NotReady node fails only itself even when every node in the batch fails that way: "all failed" resembles a cluster problem but is not evidence of one, and `_is_cluster_level` stays keyed on certain signals (connection errors, 401/403) rather than inference. A node absent from the listing is left to the patch so it produces a real 404 instead of a fabricated readiness verdict.
- `cordon_many` / `uncordon_many` are the batch equivalents, both thin wrappers over `_batch_set_unschedulable`. The batch loop is **sequential** (Kubernetes has no transaction across N node patches, and a single patch is cheap) and de-duplicates node names so `results` is safe to key by name.
- **Failure layering is the load-bearing rule for batches.** A per-node failure is collected into `results` and never aborts the batch; a *cluster*-level failure propagates as an exception so the caller sees one error instead of N identical ones. `_is_cluster_level` decides which is which: connection errors (tagged `cluster_level` by `_connection_error`) and 401/403 from the API server. When adding a new failure mode, decide which side it belongs on — getting this wrong reports "your credentials are dead" as "these 8 nodes are broken".
- `drain` always skips DaemonSet pods, mirror/static pods, and completed/failed pods (not user-configurable). Eviction honours PDBs by default; pass `disable_eviction=true` in `DrainOptions` to bypass with a raw delete. `dry_run` is resolved at the router layer and never reaches the service.
- **Drain refuses before it evicts.** Unmanaged pods (no `ownerReferences`) and emptyDir pods are *protected*: without `force` / `delete_emptydir_data` the whole drain returns 400 `DRAIN_BLOCKED` naming every blocked pod and every option needed, having evicted nothing. The node does stay cordoned — deliberately, so a corrected retry has nothing to redo (ADR 0001). Partial drains are not a thing — evicted pods cannot be recalled, so a half-drained node is worse than an untouched one. The always-skipped categories are checked first, so a DaemonSet pod using emptyDir is skipped, never blocked.
- **Outliving the drain wait budget is not an error.** `DRAIN_DEFAULT_TIMEOUT_SECONDS` bounds how long drain *watches*, not what it asks for. When it expires, drain returns 200 with `still_terminating`, `node_emptied` and `forced_deletion` — every eviction was accepted, and a pod with a long `terminationGracePeriodSeconds` is behaving as configured. There is no `DrainTimeoutException`; see `docs/adr/0001-*`.
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

**Batch endpoints** (`POST …/nodes:cordon`, `POST …/nodes:uncordon`):
- **Route convention**: collection-level actions use a **colon suffix** (`nodes:cordon`), not a path segment. `nodes/cordon` would read as "the node named cordon"; the colon marks a custom action on the collection. Single-resource actions keep the existing `nodes/{node}/cordon` form. Follow this for any new batch action.
- **Always HTTP 200** when the cluster itself was reachable, even if every node failed. The status code describes the request; per-node outcomes live in `results`, with a `summary` so callers can branch on one field. 207 Multi-Status was rejected — proxies and clients handle it inconsistently.
- Success and failure entries share **one** model (`BatchNodeResult`); success leaves the error fields unset. Don't split them into a union — it generates an awkward `anyOf` in OpenAPI-derived clients.
- Batch size is capped at 100 by the Pydantic request model, so the bound is visible in the OpenAPI schema and an oversized batch is a 422 before any work starts.
- Like every other node route, these run the service call through **`asyncio.to_thread`** — see below.

**Never call a Kubernetes service inline from a route.** `NodeService` is synchronous — the `kubernetes` SDK blocks on urllib3 sockets — so calling it directly from an `async def` handler holds the single event-loop thread for the whole operation, and the process answers nothing meanwhile, health checks included. Every node route therefore wraps its service call in `await asyncio.to_thread(svc.method, ...)`. This matters most for `drain`, whose wait budget is 25s (`DRAIN_DEFAULT_TIMEOUT_SECONDS`) and whose poll loop sleeps between attempts: inline, one drain could freeze the pod long enough for a liveness probe to restart it. Note the worker pool is bounded (FastAPI defaults to 40 threads), so this converts "everything freezes" into "long operations queue past 40 concurrent" — better, but not unbounded.

**App factory** (`app/main.py`): `create_app()` returns the FastAPI instance; the module-level `app = create_app()` line is what uvicorn targets. Swagger UI / ReDoc routes are only registered when `DEBUG=true` and serve from `app/static/docs-assets/` for offline use.

### Adding a new protected endpoint

1. Create a router in `app/api/v1/`.
2. Annotate route deps with `Depends(get_current_user(["cluster_api"]))` (or the correct scope).
3. For Kubernetes endpoints: depend on `_get_cluster_repo` → `repo.get_kube_client_config(cluster)` → `KubeClientFactory().get_core_v1(cfg)`, then pass the `CoreV1Api` into the service. **Do not import the `kubernetes` SDK from a router.**
4. Mount the router in `app/api/router.py`.
5. Add the scope to the relevant entries in `data/users.json`.
6. Wrap the service call in `await asyncio.to_thread(...)` — see **Never call a Kubernetes service inline from a route** above.
7. If the endpoint acts on many resources at once, follow the **Batch endpoints** conventions above — colon-suffix route, always-200 partial-success envelope, and a size cap on the request model.
8. If it calls a `CoreV1Api` method not used before, map it in `_PERMISSIONS` in `tests/unit/test_rbac_reference.py`, grant it in `docs/rbac/cluster-service-clusterrole.yaml` (comment which endpoint needs it), and add a row to the README's **Kubernetes Permissions** table. Local k3d runs on an admin kubeconfig, so that test is the only thing that notices a missing grant before production returns 403 — and it covers `CoreV1Api` only; using another API class (`AppsV1Api`, …) means extending the test first.

## Dry-Run Mode

`DRY_RUN_MODE=true` is a **deployment-level** switch that lets an e2e pipeline exercise the real HTTP surface without real side effects. It is shared in design with `deploy-service` — see that repo's `docs/arch/dry-run-mode.md` for the full rationale.

**Current state: both sides are stubbed.** T8 added the setting, the start-up guard and the response marker; T10 added the fake `CoreV1Api` and cluster repository, so no cluster is contacted and no kubeconfig is read; T9 added the deploy-service fakes, so no deploy-service call is made and no deploy-service token is fetched. The start-up banner states exactly this — keep it in step with reality, and note that `test_the_warning_names_what_is_still_real` exists to go red when it drifts.

### Enabling it

```bash
DRY_RUN_MODE=true APP_ENV=dev uv run uvicorn app.main:app --port 8000
```

Any `APP_ENV` except `prod` (which refuses to start — see **Invariants**). Confirm it took effect from the start-up banner, or from `"dry_run": true` on any success response.

**It needs no production secret.** No kubeconfig (nothing is read from `KUBECONFIG_BASE_PATH`), no `DEPLOY_SERVICE_PASSWORD` or `DEPLOY_SERVICE_TOKEN` (no token is fetched), and no reachable cluster or deploy-service. The one secret it does use is `SECRET_KEY`, to sign and verify its *own* JWTs — give a dry-run instance its own key, never production's, or tokens minted against it would be valid in production.

### What still runs

Only the outermost side-effecting collaborators are replaced. Everything a caller can observe a decision from is real:

- authentication and scope checks (401 / 403), Pydantic validation (422) including the batch cap of 100, enforced before any work
- every `NodeService` rule: the uncordon readiness gate (409 `NODE_NOT_READY`, no override), drain's refuse-before-evict (400 `DRAIN_BLOCKED`), the always-skipped pod categories, batch failure layering
- `PipelineService`, `CommandService`, `InventoryProxyService`, and the `DeployServiceError` code / status mapping
- the published contract: the OpenAPI document is identical to production's, as are the error envelope and the `X-Coordination-ID` → `request_id` round trip

`tests/integration/test_dry_run_deny_paths.py` holds the refusals across every router in one place. It exists because a suite asserting only 200s would pass just as well against a dry-run that skipped all of the above.

### The Kubernetes seam (T10)

Two things are replaced, both *below* `NodeService`:

- **`_get_cluster_repo`** → `DryRunClusterRepository`. Swapped first because the real repositories resolve a cluster by reading a file from `KUBECONFIG_BASE_PATH`; stubbing only the factory would still demand credentials on disk. Any cluster name resolves.
- **`KubeClientFactory.get_core_v1`** → `DryRunCoreV1Api`, a fake holding the SDK's own model objects (`V1Node`, `V1Pod`, …) rather than mocks.

`NodeService` itself is untouched, so **all of its business logic still runs**: the uncordon readiness gate (allowlist — only `"Ready"` passes), drain's refuse-before-evict check, the always-skipped pod categories, and the per-node vs cluster-level failure layering in the batch endpoints. Those refusals are the assertions worth having; a router-level short-circuit would answer 200 to every one of them. `tests/integration/test_dry_run_node_routes.py` enforces this — short-circuiting `uncordon` in the router makes the two readiness tests fail.

Two details that are load-bearing rather than cosmetic:

- **Cluster state is mutable and shared per cluster name**, cleared by `reset_dry_run_clusters()`. A cordon must still be in effect on the next request, because that is how a real cluster behaves. Call the reset in any test that mutates state — it is process-global, like `get_settings.cache_clear()`.
- **Eviction actually removes the pod.** `_wait_for_pods_gone` polls `list_pod_for_all_namespaces` until the targeted pods are gone, with a 25s budget. A fake with a fixed pod list turns every clean drain into a full-budget wait reporting `still_terminating` — a slow false failure. Removing the mutation makes the dry-run suite take ~116s instead of ~13s.

The fake's pod fixture deliberately contains one of each category drain treats differently (evictable, DaemonSet, mirror, completed, unmanaged, emptyDir, plus one on another node). Dropping any of them silently stops exercising a branch.

The ConfigMap fixture follows the same rule — one of each shape a caller branches on: data only, with `binaryData`, carrying `last-applied-configuration` (plus a second annotation, so stripping one is distinguishable from dropping all), and the same name in two namespaces. ConfigMaps are read-only, so unlike nodes and pods they are rebuilt on every call rather than held in mutable cluster state.

### The deploy-service seam (T9)

The three proxy factories — `_get_pipeline_service`, `_get_command_service`, `_get_inventory_service` — are the only construction points, so they are the seam; `PipelineService`, `CommandService` and `InventoryProxyService` are untouched.

- **`DeployServiceClient`** → `DryRunDeployServiceClient` (pipelines *and* inventory — both live on the real client). **`CommandServiceClient`** → `DryRunCommandServiceClient`; the command proxy has its own client, so it needs its own fake.
- **Not subclasses.** Inheriting would make any method the fake forgot to override fall through to real HTTP. `tests/unit/test_dry_run_deploy_clients.py` compares public signatures method by method instead — add a method to a real client and that test goes red until the fake has it too.
- **No token manager.** In dry-run the factories never call `get_deploy_token_manager()`, so the singleton is not even constructed, `DEPLOY_SERVICE_PASSWORD` is not needed and upstream `/token` is never hit. This is why the factories call it inside the live branch rather than taking it via `Depends`.
- **Errors go through the real adapter.** Where deploy-service would refuse (unknown pipeline, duplicate running pipeline, non-whitelisted command, unknown inventory node, `?format=json` on a text command, killing a non-killable command) the fakes raise `DeployServiceError` from a deploy-service-shaped body, so `_DEPLOY_CODE_MAP` / `_DEPLOY_STATUS_MAP` and the inventory 404 translation stay live. Don't replace those with direct `NotFoundException`s.
- **Lifecycle**: a pipeline or command is `running` when created and `success` from its first observation (status poll; for commands, a result *or* trace poll — the HTML viewer only polls the trace). State is process-global; clear it with `reset_dry_run_deploy_service()` / `reset_dry_run_command_service()`.
- **Fixtures**: commands `dry_run_ansible_ping` (text, killable, logged) and `dry_run_ansible_facts` (json, not killable, not logged) — one of each shape a caller branches on. Inventory answers for the same node names as `DryRunCoreV1Api` and the cluster names of `DryRunClusterRepository`.
- Identifiers are synthetic: pipeline ids ≥ 9,990,000,001, `dry-run-` command ids, `.invalid` URLs, TEST-NET-1 (`192.0.2.x`) addresses.

### Invariants

- Set by environment variable only — never a query param, header or request-body field. A per-request switch would let any caller holding a valid token make a real cordon or drain silently no-op.
- `create_app()` **raises** when `DRY_RUN_MODE=true` and `APP_ENV=prod`. A hard failure, not a warning: in production a dry-run instance answers 200 to every drain while doing nothing, and nothing alerts.
- Every `ApiResponse` carries `dry_run` (false by default, so it is not a breaking change). It is resolved by a **single hook** (`_dry_run_default` in `app/domain/models.py`) via `default_factory`, so none of the 31 `ApiResponse(...)` call sites was modified and a new one cannot forget the marker. Use `default_factory`, never `default=` — the latter binds at import and the marker then ignores any later settings change, including every test that toggles the flag.
- Error responses are built by the exception handler, not by `ApiResponse`, so they carry no marker. That is intentional; keep it that way.

### Not the same as `drain.dry_run`

The node-drain endpoint has a per-request `dry_run` body field. The two are unrelated and **must not be merged or "unified" into one flag**:

| | `drain.dry_run` | `DRY_RUN_MODE` |
|---|---|---|
| Scope | One endpoint | Whole service |
| Set by | The caller, per request | Deployment environment variable |
| Layer | Router short-circuit | Client / repository injection |
| Purpose | "Validate this drain without performing it" | "Run the e2e suite without side effects" |

They **coexist and do not interact**: with `DRY_RUN_MODE=true`, a drain sent with `dry_run: true` still short-circuits at the router and never reaches the fake (`test_request_level_drain_dry_run_still_short_circuits`); with it false, the deployment flag changes nothing about that request.

A router short-circuit is correct for `drain`: it is a caller-facing validation affordance on a single operation. It would be wrong for `DRY_RUN_MODE`, which has to leave the validation path intact in order to prove anything — a dry-run that short-circuits at the router answers 200 to everything, so the e2e suite would pass just as happily with auth deleted.

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
- Tests marked `e2e` need a live cluster and are **excluded from `make test`**; run them with `make test-e2e`. They cordon and empty a real node (`E2E_DRAIN_NODE`, default `k3d-mycluster-agent-1`), so never point them at a node running anything irreplaceable. They skip rather than fail when no cluster is reachable. See `docs/drain-e2e-testing.md`.
- The `kubernetes` SDK is **not** mocked at the SDK level — tests inject a fake `CoreV1Api`-shaped object into `NodeService`. Follow that pattern rather than patching `kubernetes.client`.

## Other directories

- `rest_client/` — `.http` files (`auth.http`, `cluster.http`, `deploy.http`) for manual API exploration in JetBrains / VS Code REST Client.
- `data/users.json` — accounts, bcrypt hashes, scopes. Use `make hash p=<password>` or `POST /api/v1/auth/hash-password` to generate hashes.
- `data/kubeconfigs/` — default `KUBECONFIG_BASE_PATH`; drop `<cluster>.yaml` or `<cluster>.json` files here for the cluster repositories to discover.

## Agent skills

### Issue tracker

Issues live as GitHub issues in `ShannonHung/Cluster-Service`, managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical triage roles, each label string equal to its name. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — `CONTEXT.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.
