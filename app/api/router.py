"""
app/api/router.py

Top-level API router.

Route layout:
  POST /token                                                  → OAuth2 token endpoint (root-level)
  GET  /api/v1/auth/verify                                     → Verify token
  POST /api/v1/auth/hash-password                              → Hash a password
  GET  /api/v1/auth/my-scopes                                  → Inspect token scopes
  POST /api/v1/deploy/stage                                    → Trigger GitLab pipeline
  POST /api/v1/deploy/stage/check-running                      → Check for duplicate running pipelines
  GET  /api/v1/deploy/stage/{id}                               → Get pipeline status
  POST /api/v1/deploy/stage/{id}/cancel                        → Cancel pipeline
  POST /api/v1/deploy/stage/{id}/retry                         → Retry pipeline
  GET  /api/v1/clusters                                        → List registered clusters
  GET  /api/v1/clusters/{cluster}/nodes                        → List nodes in cluster
  POST /api/v1/clusters/{cluster}/nodes/{node}/cordon          → Cordon a node
  POST /api/v1/clusters/{cluster}/nodes/{node}/uncordon        → Uncordon a node
  POST /api/v1/clusters/{cluster}/nodes/{node}/drain           → Drain a node
  GET  /api/v1/clusters/{cluster}/pods                         → List pods in a namespace (filtered)
  PATCH /api/v1/clusters/{cluster}/nodes/{node}/taints         → Set or remove node taints
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.clusters import router as clusters_router
from app.api.v1.deploy import router as deploy_router
from app.api.v1.nodes import router as nodes_router
from app.api.v1.pods import router as pods_router

# ── /api/v1 sub-router ────────────────────────────────────────────────────────
v1_router = APIRouter(prefix="/api/v1")
v1_router.include_router(auth_router)     # mounts at /api/v1/auth/...
v1_router.include_router(deploy_router)   # mounts at /api/v1/deploy/...
v1_router.include_router(clusters_router) # mounts at /api/v1/clusters/...
v1_router.include_router(nodes_router)    # mounts at /api/v1/clusters/{cluster}/nodes/{node}/...
v1_router.include_router(pods_router)    # mounts at /api/v1/clusters/{cluster}/pods

# ── Root router (aggregates everything) ───────────────────────────────────────
api_router = APIRouter()
api_router.include_router(v1_router)

