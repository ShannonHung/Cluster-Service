"""
app/core/config.py

Multi-environment settings using Pydantic BaseSettings.
The active environment is selected by the APP_ENV environment variable:
  - dev   → loads .env.dev
  - prod  → loads .env.prod
  - test  → loads .env.test
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # ── App meta ──────────────────────────────────────────────────────────────
    APP_ENV: Literal["dev", "prod", "test"] = "dev"
    APP_NAME: str = "Cluster Service"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # ── JWT ───────────────────────────────────────────────────────────────────
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    # ── Storage ───────────────────────────────────────────────────────────────
    USERS_JSON_PATH: str = "data/users.json"

    # ── Kubernetes ────────────────────────────────────────────────────────────
    # Base directory containing one kubeconfig file per cluster.
    # Override at runtime: KUBECONFIG_BASE_PATH=/etc/kubeconfigs
    KUBECONFIG_BASE_PATH: str = "data/kubeconfigs"

    # Default budget (seconds) a node drain waits for pods to terminate before
    # returning a structured 504. Kept conservatively low so the app times out
    # BEFORE any front proxy would (a longer proxy timeout yields a bare 500);
    # raise this only in lockstep with the proxy's read timeout.
    DRAIN_DEFAULT_TIMEOUT_SECONDS: int = 25

    # ── Deploy Service ────────────────────────────────────────────────────────
    DEPLOY_SERVICE_URL: str = "http://localhost:8001"
    DEPLOY_SERVICE_USERNAME: str = "cluster-service"
    DEPLOY_SERVICE_PASSWORD: str = ""
    DEPLOY_SERVICE_TOKEN: str = ""  # Optional initial/cached token

    # ── Dry-run (e2e pipeline testing) ────────────────────────────────────────
    # When true, the outermost side-effecting collaborators (the deploy-service
    # HTTP client and the Kubernetes API client) are replaced with stubs, while
    # routing, auth, validation and all business logic still run. Design shared
    # with deploy-service: see its docs/arch/dry-run-mode.md.
    #
    # When on, no Kubernetes cluster and no deploy-service is contacted: the
    # node routes run against DryRunCoreV1Api (app/services/dry_run_kube_client.py)
    # and the deploy / command / inventory proxies against in-memory clients
    # (app/clients/dry_run_*_client.py).
    #
    # Deliberately an environment variable and NOT a request parameter: a
    # per-request switch would let any caller holding a valid token make a real
    # cluster mutation silently no-op. Distinct from the per-request
    # `drain.dry_run` field on the node-drain endpoint, which is a caller-facing
    # validation affordance on one operation — the two must not be unified.
    # Combining this with APP_ENV=prod is refused at startup (see app/main.py).
    DRY_RUN_MODE: bool = False

    model_config = SettingsConfigDict(
        # Load order: .env (base) → .env.{APP_ENV} (env-specific overrides).
        # Missing files are silently ignored, so a plain .env alone is enough.
        env_file=[".env", f".env.{os.getenv('APP_ENV', 'dev')}", ".env.local"],
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (loaded once per process)."""
    return Settings()
