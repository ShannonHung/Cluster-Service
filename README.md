# Cluster Service

A production-ready FastAPI boilerplate with JWT authentication, structured logging, and a clean layered architecture. Use this as the starting point for new services — no external integrations included.

---

## Features

- **JWT Auth (HS256 + bcrypt)** — scope-based access control via `Depends(get_current_user([...]))`
- **Structured logging** — every log line includes `request_id` from `X-Coordination-ID`
- **Unified response shape** — `{"data": <T>, "request_id": "..."}` / `{"error": {...}, "request_id": "..."}`
- **Clean layered architecture** — Router → Service → Repository (ABC) → Implementation
- **Multi-environment config** — `.env` + `.env.{APP_ENV}` via `pydantic-settings`
- **Dockerfile** — two-stage build, non-root user

---

## Quick Start

```bash
# 1. Install dependencies
make install

# 2. Start dev server (hot-reload, loads .env + .env.dev)
make dev

# 3. Open Swagger UI
open http://localhost:8000/docs
```

---

## Commands

| Command | Description |
|---------|-------------|
| `make install` | Install all deps (including dev) into `.venv/` |
| `make dev` | Start dev server with hot-reload (`APP_ENV=dev`) |
| `make prod` | Start production server (`APP_ENV=prod`) |
| `make start` | Start server using only `.env` (no `APP_ENV`) |
| `make test` | Run all tests |
| `make test-unit` | Run unit tests only |
| `make test-int` | Run integration tests only |
| `make test-cov` | Run all tests with coverage report |
| `make hash p=<pw>` | Generate a bcrypt hash for a password |
| `make check-users [f=<file>]` | Validate a users file (accounts + access policy) before deploying it |
| `make clean` | Remove `.venv`, caches |

Manual commands (from `cluster-service/`):

```bash
# Install
uv sync --group dev

# Dev server
APP_ENV=dev uv run uvicorn app.main:app --reload --port 8000

# All tests
APP_ENV=test uv run pytest tests/ -v

# Single test file
APP_ENV=test uv run pytest tests/unit/test_auth_service.py -v

# Coverage
APP_ENV=test uv run pytest tests/ -v --cov=app --cov-report=term-missing
```

---

## Architecture

```
router (app/api/v1/)
  └── service (app/services/)
        └── repository interface (app/repositories/*_repository.py)
              └── implementation (JsonUserRepository, ...)
```

**Key design points:**

- **Routers** handle HTTP concerns only; inject concrete repository implementations into services.
- **Services** (`AuthService`) contain all business logic and depend only on abstract `ABC` repository interfaces.
- **Repository interfaces** (`UserRepository`) define the contract; concrete implementations (`JsonUserRepository`) live in the same `repositories/` directory.
- **`app/domain/models.py`** holds all Pydantic models.

---

## API Endpoints

### Auth

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/token` | ✗ | Login — obtain a JWT access token |
| `GET` | `/api/v1/auth/verify` | ✓ | Verify token and return claims |
| `GET` | `/api/v1/auth/my-scopes` | ✓ | Inspect current token scopes |
| `POST` | `/api/v1/auth/hash-password` | ✗ | Hash a plain-text password |

### Namespaces (`cluster_api`)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/clusters/{cluster}/namespaces` | List namespaces; filter by `name` (prefixes), `status` (`Active` / `Terminating`), `label_selector`. Returns every match (paging is internal) |
| `GET` | `/api/v1/clusters/{cluster}/namespaces/{name}` | Read one namespace; 404 `NAMESPACE_NOT_FOUND` |
| `POST` | `/api/v1/clusters/{cluster}/namespaces` | Create a namespace from `{"name": "...", "labels": {...}, "annotations": {...}}` (`labels` and `annotations` optional); 201. `default` / `kube-*`, non-DNS-1123 names, malformed labels or annotation keys, label or annotation keys under `kubernetes.io` / `k8s.io` (or a subdomain), label or annotation keys under `PROVENANCE_ANNOTATION_PREFIX` (or a subdomain) and any other body field are 422. 409 `NAMESPACE_ALREADY_EXISTS`, with `detail.status` = `Terminating` when the existing one is still being deleted |
| `PATCH` | `/api/v1/clusters/{cluster}/namespaces/{name}/labels` | `{"set": {...}, "remove": [...]}` — add/overwrite and delete labels key by key (absent key = no-op); returns the full current labels. 422 for `kubernetes.io` / `k8s.io` keys or keys under `PROVENANCE_ANNOTATION_PREFIX` (in `set` or `remove`), malformed labels, an empty or contradictory body, or `default` / `kube-*`. 404 `NAMESPACE_NOT_FOUND`. Stamps `last-modified-by` / `-at` / `last-request-id` |
| `PATCH` | `/api/v1/clusters/{cluster}/namespaces/{name}/annotations` | `{"set": {...}, "remove": [...]}` — add/overwrite and delete annotations key by key (values are free-form; absent key = no-op); returns the full current annotations, less `last-applied-configuration`. 422 for `kubernetes.io` / `k8s.io` keys or keys under `PROVENANCE_ANNOTATION_PREFIX` (in `set` or `remove`), malformed keys, an empty or contradictory body, or `default` / `kube-*`. 404 `NAMESPACE_NOT_FOUND`. Stamps `last-modified-by` / `-at` / `last-request-id`. Gated by the `namespace.annotate` policy action |

There is no delete endpoint: deleting a namespace takes everything in it.
A created namespace is stamped with **provenance annotations** under
`PROVENANCE_ANNOTATION_PREFIX`: `<prefix>/created-by` (the token's account),
`<prefix>/created-at` (UTC) and `<prefix>/last-request-id` (`X-Coordination-ID`);
a label or annotation update stamps `<prefix>/last-modified-by`, `<prefix>/last-modified-at`
and `last-request-id`, never touching `created-*`.
They are a pointer back to the logs, not an audit record — anyone with direct
cluster access can change them (ADR 0002). The prefix is the team's own: no
caller can set or remove a label **or** annotation key under it, so a label
already left there can only be removed with `kubectl`.

> ⚠️ A new namespace grants cluster-service **nothing** inside it. Its
> ConfigMaps stay unreadable until that namespace is given the per-namespace
> ConfigMap Role + RoleBinding (see below) — deliberately, so creating a
> namespace is never a way to widen what the service can read.

### Response shapes

**Success:**
```json
{
  "data": { ... },
  "request_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

**Error:**
```json
{
  "error": {
    "code": "AUTH_ERROR",
    "message": "Invalid account or password."
  },
  "request_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

---

## Configuration

Settings are loaded from `.env` then `.env.{APP_ENV}` (override order). The `APP_ENV` env var selects the active environment.

| File | Purpose |
|------|---------|
| `.env` | Base values (always loaded) |
| `.env.dev` | Dev overrides (`DEBUG=true`, etc.) |
| `.env.prod` | Prod overrides (replace `SECRET_KEY`; `PROVENANCE_ANNOTATION_PREFIX` is deliberately absent — set it in the deployment's environment) |
| `.env.test` | Test overrides (short token TTL, fixture user path) |

Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_ENV` | `dev` | Active environment: `dev`, `prod`, `test` |
| `APP_NAME` | `Cluster Service` | Application name shown in logs/docs |
| `SECRET_KEY` | *(required)* | JWT signing secret — **must be changed in prod** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | JWT TTL in minutes |
| `USERS_JSON_PATH` | `data/users.json` | Path to the user store and access policy |
| `PROVENANCE_ANNOTATION_PREFIX` | *(required)* | Domain your organisation controls (e.g. `cluster-service.acme.io`, no trailing `/`) that the service's namespace annotations live under. Startup fails if unset, not a DNS subdomain, or under `kubernetes.io` / `k8s.io`. Only `.env.dev` / `.env.test` set one — **production must supply its own** through the environment; changing it later leaves old annotations behind |

---

## User Management

Users are stored in the file at `USERS_JSON_PATH` (default `data/users.json`), next to the **access policy**. **[`data/users.example.json`](data/users.example.json) is a complete, commented example** — start from it.

```json
{
    "policies": [],
    "users": [
        {
            "account": "admin",
            "hashed_password": "<bcrypt hash>",
            "scopes": ["cluster_api", "vm_api"]
        }
    ]
}
```

The older bare-list form (`[ {user}, ... ]`) is still read, as a file with no policy.

**The whole file is validated when it is loaded** — at startup, and again whenever it changes on disk — not at the first login that happens to hit a bad entry. Startup fails on an invalid file; an invalid edit made while running refuses login and every policed write with `500 USERS_FILE_INVALID` until it is fixed (the last good version is never used in its place). What is checked:

- each user has a non-empty `account`, a `hashed_password` that is a **bcrypt hash** (a pasted plaintext password is refused), and `scopes` as a list; no account appears twice
- every policy rule is valid (see below) and every rule `id` is unique across the file
- no unknown keys anywhere — a typo such as `"polices"` would otherwise silently drop that user's rules. For notes, use `"$comment"`, accepted on the file, on each user and on each rule

Check a file before deploying it — every problem is listed with where it is, and the exit code is non-zero:

```bash
make check-users                         # data/users.json
make check-users f=path/to/users.json
# INVALID path/to/users.json
#   users.0.hashed_password: is not a bcrypt hash — generate one with `make hash p=<password>`
#   users.0.polices: Extra inputs are not permitted
#   users: duplicate account(s): ['alice']
```

Every problem is listed in one run, duplicates included.

### Access policy

Scopes decide *which endpoints* a token may call; the access policy decides *which clusters, nodes and namespaces* a write may touch. Rules go in the top-level `policies` (every user) or in a user's own `policies`.

A rule:

| Field | Required | Meaning |
|-------|----------|---------|
| `id` | yes | Unique name; shown in every denial and audit log line |
| `description` | no | Why the rule exists; shown to the caller who is refused |
| `actions` | yes | Which policed actions it covers — names from the table below, or a glob: `node.*`, `namespace.*`, `*` |
| `effect` | yes | `deny` or `allow` |
| `match` | yes | Parameter → regex. **All** entries must match (AND). Each regex must match the **whole** value: `prod` does not match `nonprod-1`; write `prod-.*` for a prefix. Use `.*` to match anything |
| `$comment` | no | Free text, ignored |

```json
{ "id": "no-prod-writes",
  "description": "prod clusters are maintained by the SRE pipeline",
  "actions": ["node.*", "namespace.*"],
  "effect": "deny",
  "match": { "cluster": "prod-.*" } }
```

#### Where action names come from

An action is a fixed name for one kind of write, `<resource>.<verb>`. The list is defined in code — the `PolicyAction` enum and, with the parameters each action can be matched on, `POLICY_ACTIONS`, both in [`app/domain/access_policy.py`](app/domain/access_policy.py). A rule names an action by its value (`"node.drain"`), not by the enum member. Each name maps to the endpoints below, and the parameter values are taken from the request: `cluster` and `node` from the URL path, `namespace` from the path (label or annotation update) or from the body's `name` (create).

| Action | `match` keys | Endpoints it covers |
|--------|--------------|---------------------|
| `node.cordon` | `cluster`, `node` | `POST /api/v1/clusters/{cluster}/nodes/{node}/cordon`, `POST /api/v1/clusters/{cluster}/nodes:cordon` (each node of the batch) |
| `node.uncordon` | `cluster`, `node` | `POST …/nodes/{node}/uncordon`, `POST …/nodes:uncordon` (each node) |
| `node.drain` | `cluster`, `node` | `POST …/nodes/{node}/drain` — also when `dry_run=true`, so a preview reports the refusal |
| `node.label` | `cluster`, `node` | `PATCH …/nodes/{node}/labels` |
| `node.annotate` | `cluster`, `node` | `PATCH …/nodes/{node}/annotations` |
| `node.taint` | `cluster`, `node` | `PATCH …/nodes/{node}/taints` |
| `namespace.create` | `cluster`, `namespace` | `POST /api/v1/clusters/{cluster}/namespaces` (`namespace` = body `name`) |
| `namespace.label` | `cluster`, `namespace` | `PATCH /api/v1/clusters/{cluster}/namespaces/{name}/labels` |
| `namespace.annotate` | `cluster`, `namespace` | `PATCH /api/v1/clusters/{cluster}/namespaces/{name}/annotations` |

> ⚠️ **Upgrading to a version with `namespace.annotate`.** An action that no rule
> names is allowed, so a rule written for `namespace.label` does **not** cover
> annotating: after the upgrade an account limited to labelling a namespace (or
> denied the platform ones, `cattle-*`, `istio-*`, …) can still annotate it.
> Before deploying, add `namespace.annotate` beside `namespace.label` in every
> rule that names it, or in a rule of its own; `make check-users` will not flag
> the gap.

Only writes are policed — every `GET` is unaffected, and so are the deploy / command / inventory proxies (deploy-service restricts those itself). A name not in this table, or a `match` key the rule's actions do not have (e.g. `node` on `namespace.create`), makes the file invalid rather than leaving an inert rule. A new write endpoint gets its action added to `PolicyAction` / `POLICY_ACTIONS` and to this table.

#### How a request is judged

Each request is judged by two **layers** — the global `policies`, then the caller's own — and must pass **both**. Within one layer, for the action being requested:

1. if any `deny` rule matches → **refused**;
2. else, if the layer has at least one `allow` rule for this action → one of them must match, or **refused**;
3. else → passes this layer.

So the default is **open**: rules only ever take permissions away.

| The layer has, for this action… | Request is… |
|-------------------------------|-------------|
| no rules at all | allowed |
| only `deny` rules | allowed unless a `deny` matches |
| only `allow` rules | allowed only if an `allow` matches |
| both | refused if a `deny` matches (deny wins); otherwise allowed only if an `allow` matches |

An `allow` rule restricts **only the actions it lists**: an operator allow-listed for `node.drain` on `dev-.*` is not restricted on `node.taint` by it. Because both layers must pass, a user's rules can only **tighten** the global ones — a user `allow` never overrides a global `deny`, and nobody, `admin` included, is exempt from a global rule. A rule whose `match` names a parameter the request lacks does not match.

#### What the caller sees

- A refused single-resource write: `403 POLICY_DENIED`, nothing sent to Kubernetes. The message names the rule and its `description`; `detail` holds `action`, `layer` (`global` / `user`), `rule_id` (`null` when no `allow` matched) and `params`.
- A refused node in a batch: an ordinary `"status": "failed"` entry in `results`, counted in `summary.failed`, with `error_code: POLICY_DENIED`, the reason in `message` and no `kube_status` (it was never sent to Kubernetes). The other nodes proceed, and the response is still 200 — the batch response shape is unchanged. A fully refused batch never contacts the cluster.

Edits apply on the next request; no restart is needed. Design: [ADR 0003](docs/adr/0003-access-policy-lives-in-the-users-file-and-only-tightens.md).

Generate a password hash:

```bash
make hash p=mypassword
# or
POST /api/v1/auth/hash-password  {"password": "mypassword"}
```

---

## Kubernetes Permissions (RBAC)

The credentials cluster-service uses for each managed cluster need the
permissions below. A reference ServiceAccount + ClusterRole + ClusterRoleBinding
(nodes, pods) and a Role + RoleBinding (ConfigMaps, per namespace) live in
[`docs/rbac/cluster-service-clusterrole.yaml`](docs/rbac/cluster-service-clusterrole.yaml);
apply it to every managed cluster and use that ServiceAccount's token.

ConfigMaps follow least privilege: they are readable only in namespaces that
carry the Role + RoleBinding (the reference uses `gpu-operator`; repeat the
pair for each namespace to be readable). As a result `GET /configmaps?namespace=*`
— which needs a cluster-wide `list` — is refused with **403**; list a granted
namespace instead.

> ⚠️ Local k3d uses an **admin** kubeconfig, so a missing permission never
> fails locally — it only shows up as a 403 in production.
> `tests/unit/test_rbac_reference.py` keeps the reference file in step with the
> code, but cannot check what is actually applied to your clusters.

| Endpoint | Resource | Verbs |
|----------|----------|-------|
| `GET /clusters/{cluster}/nodes` | `nodes` | `list` |
| `GET /clusters/{cluster}/nodes/{node}` | `nodes` | `get` |
| `POST /clusters/{cluster}/nodes/{node}/cordon` | `nodes` | `patch` |
| `POST /clusters/{cluster}/nodes/{node}/uncordon` | `nodes` | `get`, `patch` |
| `POST /clusters/{cluster}/nodes:cordon` | `nodes` | `patch` |
| `POST /clusters/{cluster}/nodes:uncordon` | `nodes` | `list`, `patch` |
| `POST /clusters/{cluster}/nodes/{node}/drain` | `nodes` | `patch` |
| | `pods` | `list`, `delete` (`delete` only with `disable_eviction=true`) |
| | `pods/eviction` | `create` |
| `PATCH /clusters/{cluster}/nodes/{node}/labels` | `nodes` | `get`, `patch` |
| `PATCH /clusters/{cluster}/nodes/{node}/annotations` | `nodes` | `get`, `patch` |
| `PATCH /clusters/{cluster}/nodes/{node}/taints` | `nodes` | `get`, `patch` |
| `GET /clusters/{cluster}/pods` | `pods` | `list` |
| `GET /clusters/{cluster}/configmaps` | `configmaps` | `list` (per namespace via Role; `namespace=*` needs it cluster-wide) |
| `GET /clusters/{cluster}/namespaces/{namespace}/configmaps/{name}` | `configmaps` | `get` (per namespace via Role) |
| `GET /clusters/{cluster}/namespaces` | `namespaces` | `list` |
| `GET /clusters/{cluster}/namespaces/{name}` | `namespaces` | `get` |
| `POST /clusters/{cluster}/namespaces` | `namespaces` | `create`, `get` (`get` only on 409, to tell a Terminating namespace apart) |
| `PATCH /clusters/{cluster}/namespaces/{name}/labels` | `namespaces` | `patch` |
| `PATCH /clusters/{cluster}/namespaces/{name}/annotations` | `namespaces` | `patch` |

All paths are under `/api/v1`. `GET /clusters` reads local config only and
needs no Kubernetes permission.

---

## Adding a New Protected Endpoint

1. Create a router in `app/api/v1/`.
2. Add `dependencies=[Depends(get_current_user(["your_scope"]))]` to the `APIRouter`.
3. Mount it in `app/api/router.py`.
4. Add the scope to relevant users in `data/users.json`.
5. If it calls a `CoreV1Api` method not used before, map it in `_PERMISSIONS` in `tests/unit/test_rbac_reference.py`, grant it in `docs/rbac/cluster-service-clusterrole.yaml`, and add a row to the table above.

---

## Tests

- `tests/conftest.py` sets `APP_ENV=test` and provides a session-scoped `TestClient`.
- `tests/fixtures/users.json` is the user store in test mode (password: `secret`).
- Unit tests mock repository dependencies; integration tests use the full `TestClient`.
- `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed.

---

## Logging

Every log line includes the request ID:

```
2026-03-08T10:00:00 | INFO     | req=trace-abc-123 | app.services.auth_service:57 | Authentication successful | account=admin
```

Pass `X-Coordination-ID: <your-id>` in requests to propagate trace IDs across services. The same value is echoed back in the response header.
