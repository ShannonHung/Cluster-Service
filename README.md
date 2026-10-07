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
| `.env.prod` | Prod overrides (replace `SECRET_KEY`) |
| `.env.test` | Test overrides (short token TTL, fixture user path) |

Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_ENV` | `dev` | Active environment: `dev`, `prod`, `test` |
| `APP_NAME` | `Cluster Service` | Application name shown in logs/docs |
| `SECRET_KEY` | *(required)* | JWT signing secret — **must be changed in prod** |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `60` | JWT TTL in minutes |
| `USERS_JSON_PATH` | `data/users.json` | Path to the user store |

---

## User Management

Users are stored in `data/users.json`:

```json
[
    {
        "account": "admin",
        "hashed_password": "<bcrypt hash>",
        "scopes": ["cluster_api", "vm_api"]
    }
]
```

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
lives in [`docs/rbac/cluster-service-clusterrole.yaml`](docs/rbac/cluster-service-clusterrole.yaml);
apply it to every managed cluster and use that ServiceAccount's token.

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
| `GET /clusters/{cluster}/configmaps` | `configmaps` | `list` |

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
