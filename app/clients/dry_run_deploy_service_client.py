"""
app/clients/dry_run_deploy_service_client.py

A stand-in for ``DeployServiceClient`` used when ``DRY_RUN_MODE=true``.

Why this seam. The deploy and inventory routes run:

    _get_pipeline_service() / _get_inventory_service()  — the only construction points
    PipelineService / InventoryProxyService              — orchestration, untouched
    DeployServiceClient                                  — HTTP to deploy-service

Dry-run replaces only the last one, so auth, scope checks, Pydantic validation
and the services above it all still run. No new seam was needed: both services
already take their client by constructor injection.

**Not a subclass, on purpose.** Inheriting from ``DeployServiceClient`` would
make every method this class forgot to override silently fall through to the
real HTTP implementation — exactly the leak dry-run exists to prevent. The
shared public surface is enforced by ``tests/unit/test_dry_run_deploy_clients.py``
instead, which compares signatures method by method.

**No token is ever fetched.** This client takes no ``TokenManager``; the route
factories do not even construct the shared ``DeployServiceTokenManager``
singleton in dry-run, so ``DEPLOY_SERVICE_PASSWORD`` is not needed and the
upstream ``/token`` endpoint is never called.

**Errors go through the real adapter.** Where deploy-service would refuse —
an unknown pipeline id, a duplicate running pipeline, an unknown inventory
node — this raises ``DeployServiceError`` built from a deploy-service-shaped
error body, exactly as ``DeployServiceClient._raise_for_error`` does. The
``_DEPLOY_CODE_MAP`` / ``_DEPLOY_STATUS_MAP`` adaptation therefore stays on a
live path in dry-run instead of becoming dead code, and so does
``InventoryProxyService``'s upstream-404 → 404 translation.

**State is process-global, like the Kubernetes fake.** A triggered pipeline
must still exist on the following status poll, and a cancel must still be in
effect after it, so pipelines live in a module-level store shared by the
per-request client instances. ``reset_dry_run_deploy_service()`` clears it.

Lifecycle, chosen so an e2e "trigger, then poll until terminal" loop finishes:
a pipeline is ``running`` when triggered and reports ``success`` from its first
status poll onward. Cancel takes a running pipeline to ``canceled``; retry takes
a ``canceled`` / ``failed`` one back to ``running``. Acting on a pipeline that
is already in a state the action does not apply to returns it unchanged, as
GitLab does.

Every identifier is deliberately synthetic: pipeline ids sit in a range no
real project reaches, URLs use the reserved ``.invalid`` TLD (RFC 2606) and
addresses use TEST-NET-1 (RFC 5737), so a value that leaks into a real system
fails loudly instead of colliding with a genuine record.

Design shared with deploy-service; see that repo's docs/arch/dry-run-mode.md.
"""

from __future__ import annotations

import itertools
import logging
from datetime import datetime, timezone
from typing import Any

from app.core.exceptions import DeployServiceError
from app.domain.inventory_models import (
    BastionMapping,
    ClusterBastionResolution,
    ClusterNodeInfo,
    ClusterRef,
    NodeBastionResolution,
    NodeInfo,
)
from app.domain.pipeline_models import (
    JobData,
    PipelineData,
    PipelineVariable,
    RunningPipelinesData,
)
from app.repositories.dry_run_cluster_repository import DRY_RUN_CLUSTERS
from app.services.dry_run_kube_client import DRY_RUN_NODES

_logger = logging.getLogger(__name__)

# Nine billion and up: far beyond any real GitLab instance's pipeline ids, and
# visibly patterned when it turns up in a log.
_PIPELINE_ID_BASE = 9_990_000_000
_WEB_BASE = "https://dry-run.invalid"

# Statuses check-running treats as "still going", mirroring deploy-service.
_ACTIVE = frozenset({"created", "pending", "running"})

DRY_RUN_BASTION_TYPE = "dry-run"
_MAPPING = BastionMapping(
    patterns=["dry-run-.*"],
    runner="dry-run-runner",
    bastion="dry-run-bastion",
    bastion_ip="192.0.2.1",
)


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _error(status: int, code: str, message: str, detail: Any = None) -> DeployServiceError:
    """Build the error deploy-service would have sent, via the real adapter."""
    block: dict[str, Any] = {"code": code, "message": f"dry-run: {message}"}
    if detail is not None:
        block["detail"] = detail
    return DeployServiceError(http_status=status, body={"error": block})


class _PipelineStore:
    """Pipelines triggered against this process, keyed by id."""

    def __init__(self) -> None:
        self.pipelines: dict[int, PipelineData] = {}
        # Polls observed per pipeline, to advance running → success.
        self.polls: dict[int, int] = {}
        self._ids = itertools.count(_PIPELINE_ID_BASE + 1)

    def next_id(self) -> int:
        return next(self._ids)


_STORE = _PipelineStore()


def reset_dry_run_deploy_service() -> None:
    """Drop all dry-run pipeline state.

    Process-global, so a test that triggers a pipeline must clear it — exactly
    as it would call ``get_settings.cache_clear()`` or
    ``reset_dry_run_clusters()``.
    """
    global _STORE
    _STORE = _PipelineStore()


def _variables(action: str, variables: list[PipelineVariable]) -> list[PipelineVariable]:
    # deploy-service forwards `action` as the EXECUTION variable.
    return [PipelineVariable(key="EXECUTION", value=action), *variables]


def _same_variables(a: list[PipelineVariable], b: list[PipelineVariable]) -> bool:
    return sorted((v.key, v.value) for v in a) == sorted((v.key, v.value) for v in b)


class DryRunDeployServiceClient:
    """In-memory stand-in for ``DeployServiceClient``. Opens no connection."""

    # ── pipelines ─────────────────────────────────────────────────────────────

    async def trigger_pipeline(
        self,
        action: str,
        ref_name: str,
        variables: list[PipelineVariable],
    ) -> PipelineData:
        """Record a new running pipeline.

        Refuses a duplicate of one already running with the CONFLICT error
        deploy-service sends, so a caller's duplicate-handling path is
        reachable in dry-run rather than only in production.
        """
        full = _variables(action, variables)
        running = self._matching_running(ref_name, full)
        if running:
            raise _error(
                409,
                "CONFLICT",
                f"an identical pipeline is already running on '{ref_name}'",
                detail={"pipeline_ids": [p.id for p in running]},
            )

        pipeline_id = _STORE.next_id()
        now = _now()
        pipeline = PipelineData(
            id=pipeline_id,
            status="running",
            created_at=now,
            updated_at=now,
            started_at=now,
            tag_list=["dry-run"],
            variables=full,
            jobs=[JobData(id=pipeline_id * 10 + 1, name="dry-run-job", status="running")],
            ref_name=ref_name,
            web_url=f"{_WEB_BASE}/pipelines/{pipeline_id}",
        )
        _STORE.pipelines[pipeline_id] = pipeline
        _STORE.polls[pipeline_id] = 0
        _logger.warning(
            "DRY-RUN | op=deploy.trigger_pipeline | action=%s | ref=%s | id=%s | "
            "no pipeline was triggered",
            action, ref_name, pipeline_id,
        )
        return pipeline.model_copy(deep=True)

    async def check_running(
        self,
        action: str,
        ref_name: str,
        variables: list[PipelineVariable],
    ) -> RunningPipelinesData:
        running = self._matching_running(ref_name, _variables(action, variables))
        _logger.warning(
            "DRY-RUN | op=deploy.check_running | action=%s | ref=%s | matches=%d",
            action, ref_name, len(running),
        )
        return RunningPipelinesData(
            has_running=bool(running),
            count=len(running),
            pipelines=[p.model_copy(deep=True) for p in running],
        )

    async def get_pipeline(self, pipeline_id: int) -> PipelineData:
        """Return the pipeline, advancing ``running`` → ``success`` on the
        first poll so an e2e poll-until-terminal loop terminates."""
        pipeline = self._get(pipeline_id)
        _STORE.polls[pipeline_id] += 1
        if pipeline.status == "running":
            self._set_status(pipeline, "success", finished=True)
        _logger.warning(
            "DRY-RUN | op=deploy.get_pipeline | id=%s | status=%s",
            pipeline_id, pipeline.status,
        )
        return pipeline.model_copy(deep=True)

    async def cancel_pipeline(self, pipeline_id: int) -> PipelineData:
        pipeline = self._get(pipeline_id)
        if pipeline.status in _ACTIVE:
            self._set_status(pipeline, "canceled", finished=True)
        _logger.warning(
            "DRY-RUN | op=deploy.cancel_pipeline | id=%s | status=%s | "
            "no pipeline was cancelled",
            pipeline_id, pipeline.status,
        )
        return pipeline.model_copy(deep=True)

    async def retry_pipeline(self, pipeline_id: int) -> PipelineData:
        pipeline = self._get(pipeline_id)
        if pipeline.status in {"canceled", "failed"}:
            self._set_status(pipeline, "running", finished=False)
        _logger.warning(
            "DRY-RUN | op=deploy.retry_pipeline | id=%s | status=%s | "
            "no pipeline was retried",
            pipeline_id, pipeline.status,
        )
        return pipeline.model_copy(deep=True)

    # ── inventory ─────────────────────────────────────────────────────────────
    # Node names match DryRunCoreV1Api's nodes, so an e2e test can move between
    # the inventory and node endpoints with the same identifiers.

    async def get_node(self, node_name: str) -> ClusterNodeInfo:
        _logger.warning("DRY-RUN | op=inventory.get_node | node=%s", node_name)
        node, cluster = self._inventory_node(node_name)
        return ClusterNodeInfo(node_type="dry-run", node=node, cluster=cluster)

    async def list_mappings(self, type_name: str) -> list[BastionMapping]:
        _logger.warning("DRY-RUN | op=inventory.list_mappings | type=%s", type_name)
        if type_name != DRY_RUN_BASTION_TYPE:
            raise _error(404, "NOT_FOUND", f"no bastion mappings for type '{type_name}'")
        return [_MAPPING.model_copy()]

    async def resolve_node_bastion(
        self, node_name: str, bastion_type: str | None = None
    ) -> NodeBastionResolution:
        _logger.warning(
            "DRY-RUN | op=inventory.resolve_node_bastion | node=%s | type=%s",
            node_name, bastion_type,
        )
        node, cluster = self._inventory_node(node_name)
        resolved_type = bastion_type or DRY_RUN_BASTION_TYPE
        if resolved_type != DRY_RUN_BASTION_TYPE:
            raise _error(404, "NOT_FOUND", f"no bastion mappings for type '{resolved_type}'")
        return NodeBastionResolution(
            node_type="dry-run",
            node=node,
            cluster=cluster,
            bastion_type=resolved_type,
            bastion_type_source="query_param" if bastion_type else "config",
            matched_mapping=_MAPPING.model_copy(),
            matched_pattern=_MAPPING.patterns[0],
        )

    async def resolve_cluster_bastion(
        self, cluster_name: str
    ) -> ClusterBastionResolution:
        _logger.warning(
            "DRY-RUN | op=inventory.resolve_cluster_bastion | cluster=%s", cluster_name
        )
        if cluster_name not in DRY_RUN_CLUSTERS:
            raise _error(404, "NOT_FOUND", f"cluster '{cluster_name}' not in inventory")
        return ClusterBastionResolution(
            cluster_name=cluster_name,
            has_slash="/" in cluster_name,
            bastion_type=DRY_RUN_BASTION_TYPE,
            matched_mapping=_MAPPING.model_copy(),
            matched_pattern=_MAPPING.patterns[0],
        )

    # ── internals ─────────────────────────────────────────────────────────────

    @staticmethod
    def _get(pipeline_id: int) -> PipelineData:
        pipeline = _STORE.pipelines.get(pipeline_id)
        if pipeline is None:
            raise _error(404, "NOT_FOUND", f"pipeline {pipeline_id} not found")
        return pipeline

    @staticmethod
    def _matching_running(
        ref_name: str, variables: list[PipelineVariable]
    ) -> list[PipelineData]:
        return [
            p for p in _STORE.pipelines.values()
            if p.status in _ACTIVE
            and p.ref_name == ref_name
            and _same_variables(p.variables, variables)
        ]

    @staticmethod
    def _set_status(pipeline: PipelineData, status: str, *, finished: bool) -> None:
        now = _now()
        pipeline.status = status
        pipeline.updated_at = now
        pipeline.finished_at = now if finished else None
        for job in pipeline.jobs:
            job.status = status

    @staticmethod
    def _inventory_node(node_name: str) -> tuple[NodeInfo, ClusterRef]:
        if node_name not in DRY_RUN_NODES:
            raise _error(404, "NOT_FOUND", f"node '{node_name}' not in inventory")
        cluster = DRY_RUN_CLUSTERS[0]
        return (
            NodeInfo(
                id=f"dry-run-id-{node_name}",
                name=node_name,
                labels={"dry-run": "true"},
            ),
            ClusterRef(id=f"dry-run-id-{cluster}", name=cluster, context=cluster),
        )
