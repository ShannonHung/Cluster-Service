# Pods Query API + Slim Node Detail Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dedicated, filterable pods query endpoint and strip pods out of the node-detail response.

**Architecture:** A new `GET /api/v1/clusters/{cluster}/pods` route (own `pods.py` router) flows through the existing repo → KubeClientFactory → `NodeService` chain. A new `NodeService.list_pods` method calls `list_namespaced_pod(namespace)` for a single namespace (or `list_pod_for_all_namespaces()` when `namespace == "*"`) and applies node / status / pod_name filters in Python. `PodInfo` gains `node_name`; `NodeDetailData` loses `pods`.

**Tech Stack:** FastAPI, Pydantic, kubernetes Python SDK, pytest (asyncio_mode=auto).

**Run all commands from `cluster-service/`. Tests use `APP_ENV=test`.**

---

### Task 1: Model changes — PodInfo.node_name, PodListData, drop NodeDetailData.pods

**Files:**
- Modify: `app/domain/kubernetes_models.py`

- [ ] **Step 1: Add `node_name` to `PodInfo`**

In `app/domain/kubernetes_models.py`, change the `PodInfo` class (currently ends at `restart_count`) to add a `node_name` field:

```python
class PodInfo(BaseModel):
    """Summary of a pod running on a node."""

    name: str
    namespace: str
    phase: str                        # Running | Pending | Succeeded | Failed | Unknown
    ready: bool = False               # True when all containers are Ready
    owner_kind: Optional[str] = None  # ReplicaSet | DaemonSet | StatefulSet | Job | None
    restart_count: int = 0            # sum of restarts across all containers
    node_name: str = ""               # node the pod is scheduled on (spec.nodeName)
```

- [ ] **Step 2: Add `PodListData` model**

Directly after `PodInfo`, add:

```python
class PodListData(BaseModel):
    """Response body for GET /api/v1/clusters/{cluster}/pods."""

    cluster: str
    namespace: str
    pods: list[PodInfo] = Field(default_factory=list)
```

- [ ] **Step 3: Remove `pods` from `NodeDetailData`**

In the `NodeDetailData` class, delete the `pods` field line and update the docstring:

```python
class NodeDetailData(BaseModel):
    """Full node detail (node attributes only; pods are queried separately).

    Used by GET …/{cluster}/nodes/{node}.
    Shares the same leaf fields as NodeInfo, adding annotations.
    """

    cluster: str
    name: str
    status: str
    roles: list[str] = Field(default_factory=list)
    version: str = ""
    unschedulable: bool = False
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
```

- [ ] **Step 4: Verify it imports**

Run: `APP_ENV=test uv run python -c "from app.domain.kubernetes_models import PodInfo, PodListData, NodeDetailData; print(PodInfo().model_fields.keys() if False else 'ok')"`
Expected: prints `ok` (no ImportError). If it complains about required fields, that's fine — we only care there is no ImportError. Simpler: `APP_ENV=test uv run python -c "import app.domain.kubernetes_models as m; assert 'node_name' in m.PodInfo.model_fields; assert hasattr(m, 'PodListData'); assert 'pods' not in m.NodeDetailData.model_fields; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 5: Commit**

```bash
git add app/domain/kubernetes_models.py
git commit -m "feat: add PodListData, PodInfo.node_name; drop NodeDetailData.pods"
```

---

### Task 2: `_pod_to_info` sets node_name + update `get_node` to drop pods

**Files:**
- Modify: `app/services/node_service.py`

- [ ] **Step 1: Update the failing test for `get_node` (no pods)**

In `tests/unit/test_node_service.py`, add this test in the list_nodes/get_node area (near the top section). First, the existing `_make_pod` helper does not set `spec.node_name`; we extend it in Task 3. For now add:

```python
def test_get_node_returns_detail_without_pods():
    kube = _make_kube()
    kube.read_node.return_value = _make_node("worker-1", labels={"env": "prod"})
    result = _svc().get_node(cluster="test", node_name="worker-1", kube=kube)

    assert result.cluster == "test"
    assert result.name == "worker-1"
    assert result.labels == {"env": "prod"}
    # Node detail must NOT list pods anymore, and must not query pods.
    assert not hasattr(result, "pods")
    kube.list_pod_for_all_namespaces.assert_not_called()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `APP_ENV=test uv run pytest tests/unit/test_node_service.py::test_get_node_returns_detail_without_pods -v`
Expected: FAIL — `get_node` still calls `list_pod_for_all_namespaces` and `NodeDetailData` may still carry `pods`.

- [ ] **Step 3: Rewrite `get_node` to drop pods**

In `app/services/node_service.py`, replace the entire `get_node` method body (the `# ── Single node detail ──` section) with:

```python
    def get_node(self, cluster: str, node_name: str, kube: CoreV1Api) -> NodeDetailData:
        """Fetch full detail for a single node (node attributes only).

        Pods are queried via the dedicated pods endpoint, not here.

        Raises:
            NodeNotFoundException: If the node does not exist.
            KubeApiException: On Kubernetes API failure.
        """
        try:
            node = kube.read_node(node_name)
        except ApiException as exc:
            if exc.status == 404:
                raise NodeNotFoundException(
                    f"Node '{node_name}' not found in cluster '{cluster}'.",
                ) from exc
            raise KubeApiException(
                f"Failed to read node '{node_name}': {exc.reason}",
                kube_status=exc.status,
            ) from exc

        info = self._node_to_info(node)
        _logger.info("Got node detail | cluster=%s | node=%s", cluster, node_name)
        return NodeDetailData(
            cluster=cluster,
            name=info.name,
            status=info.status,
            roles=info.roles,
            version=info.version,
            unschedulable=info.unschedulable,
            labels=info.labels,
            annotations=info.annotations,
        )
```

- [ ] **Step 4: Set `node_name` in `_pod_to_info`**

In `app/services/node_service.py`, update the `_pod_to_info` static method's return to include `node_name`:

```python
        return PodInfo(
            name=pod.metadata.name,
            namespace=pod.metadata.namespace,
            phase=pod.status.phase or "Unknown",
            ready=ready,
            owner_kind=owner_kind,
            restart_count=restart_count,
            node_name=(pod.spec.node_name or "") if pod.spec else "",
        )
```

- [ ] **Step 5: Run the get_node test to verify it passes**

Run: `APP_ENV=test uv run pytest tests/unit/test_node_service.py::test_get_node_returns_detail_without_pods -v`
Expected: PASS.

- [ ] **Step 6: Run the full node_service suite to catch regressions**

Run: `APP_ENV=test uv run pytest tests/unit/test_node_service.py -v`
Expected: All PASS. (Drain tests still use `list_pod_for_all_namespaces` — untouched.)

- [ ] **Step 7: Commit**

```bash
git add app/services/node_service.py tests/unit/test_node_service.py
git commit -m "feat: slim node detail to drop pods; pod_to_info sets node_name"
```

---

### Task 3: `NodeService.list_pods` with filters

**Files:**
- Modify: `app/services/node_service.py`
- Modify: `tests/unit/test_node_service.py`

- [ ] **Step 1: Extend the `_make_pod` test helper to support node_name + name prefix tests**

In `tests/unit/test_node_service.py`, replace the `_make_pod` helper with one that also sets `spec.node_name` and empty container_statuses (so `_pod_to_info` works against it):

```python
def _make_pod(
    name: str = "mypod",
    namespace: str = "default",
    phase: str = "Running",
    owner_kind: str = "ReplicaSet",
    is_mirror: bool = False,
    node_name: str = "worker-1",
) -> MagicMock:
    pod = MagicMock()
    pod.metadata.name = name
    pod.metadata.namespace = namespace
    pod.metadata.annotations = {"kubernetes.io/config.mirror": ""} if is_mirror else {}
    owner = MagicMock()
    owner.kind = owner_kind
    pod.metadata.owner_references = [owner]
    pod.status.phase = phase
    pod.status.container_statuses = []
    pod.spec.node_name = node_name
    return pod
```

- [ ] **Step 2: Run existing drain tests to confirm the helper change is safe**

Run: `APP_ENV=test uv run pytest tests/unit/test_node_service.py -k drain -v`
Expected: All drain tests PASS (the new attributes are additive).

- [ ] **Step 3: Write failing tests for `list_pods`**

In `tests/unit/test_node_service.py`, add a new section at the end. Also add `PodListData` to the import line from `app.domain.kubernetes_models`:

```python
# ── list_pods ─────────────────────────────────────────────────────────────────

def test_list_pods_no_filters_returns_all_in_namespace():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("web-1", node_name="n1"),
        _make_pod("api-1", node_name="n2"),
    ]
    result = _svc().list_pods(cluster="test", namespace="default", kube=kube)

    assert isinstance(result, PodListData)
    assert result.cluster == "test"
    assert result.namespace == "default"
    assert {p.name for p in result.pods} == {"web-1", "api-1"}
    kube.list_namespaced_pod.assert_called_once_with("default")
    kube.list_pod_for_all_namespaces.assert_not_called()


def test_list_pods_wildcard_lists_all_namespaces():
    kube = _make_kube()
    kube.list_pod_for_all_namespaces.return_value.items = [
        _make_pod("web-1", namespace="default", node_name="n1"),
        _make_pod("kube-dns", namespace="kube-system", node_name="n2"),
    ]
    result = _svc().list_pods(cluster="test", namespace="*", kube=kube)

    assert result.namespace == "*"
    assert {p.name for p in result.pods} == {"web-1", "kube-dns"}
    kube.list_pod_for_all_namespaces.assert_called_once_with()
    kube.list_namespaced_pod.assert_not_called()


def test_list_pods_wildcard_still_applies_filters():
    kube = _make_kube()
    kube.list_pod_for_all_namespaces.return_value.items = [
        _make_pod("web-1", namespace="default", node_name="n1", phase="Running"),
        _make_pod("web-2", namespace="kube-system", node_name="n2", phase="Pending"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="*", kube=kube, statuses=["Running"]
    )
    assert {p.name for p in result.pods} == {"web-1"}


def test_list_pods_filters_by_node():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("web-1", node_name="n1"),
        _make_pod("web-2", node_name="n2"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, nodes=["n1"]
    )
    assert {p.name for p in result.pods} == {"web-1"}


def test_list_pods_filters_by_multiple_nodes_or():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("a", node_name="n1"),
        _make_pod("b", node_name="n2"),
        _make_pod("c", node_name="n3"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, nodes=["n1", "n2"]
    )
    assert {p.name for p in result.pods} == {"a", "b"}


def test_list_pods_filters_by_status_case_insensitive():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("running-pod", phase="Running"),
        _make_pod("pending-pod", phase="Pending"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, statuses=["running"]
    )
    assert {p.name for p in result.pods} == {"running-pod"}


def test_list_pods_filters_by_name_prefix_or():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("web-7d9f"),
        _make_pod("api-xyz"),
        _make_pod("db-1"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, name_prefixes=["web-", "api-"]
    )
    assert {p.name for p in result.pods} == {"web-7d9f", "api-xyz"}


def test_list_pods_filters_are_anded():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [
        _make_pod("web-1", node_name="n1", phase="Running"),
        _make_pod("web-2", node_name="n2", phase="Running"),
        _make_pod("web-3", node_name="n1", phase="Pending"),
    ]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube,
        nodes=["n1"], statuses=["Running"], name_prefixes=["web-"],
    )
    assert {p.name for p in result.pods} == {"web-1"}


def test_list_pods_empty_when_no_match():
    kube = _make_kube()
    kube.list_namespaced_pod.return_value.items = [_make_pod("web-1", node_name="n1")]
    result = _svc().list_pods(
        cluster="test", namespace="default", kube=kube, nodes=["nonexistent"]
    )
    assert result.pods == []


def test_list_pods_raises_on_api_error():
    kube = _make_kube()
    kube.list_namespaced_pod.side_effect = _api_error(500)
    with pytest.raises(KubeApiException):
        _svc().list_pods(cluster="test", namespace="default", kube=kube)
```

Update the import line at the top of the file:

```python
from app.domain.kubernetes_models import (
    DrainActionData,
    DrainOptions,
    NodeActionData,
    NodeListData,
    PodListData,
)
```

- [ ] **Step 4: Run to verify they fail**

Run: `APP_ENV=test uv run pytest tests/unit/test_node_service.py -k list_pods -v`
Expected: FAIL — `list_pods` does not exist (AttributeError). 10 tests collected.

- [ ] **Step 5: Implement `list_pods`**

In `app/services/node_service.py`, add this method right after `list_nodes` (before the `get_node` section). Also add `PodListData` to the existing import from `app.domain.kubernetes_models`:

```python
    def list_pods(
        self,
        cluster: str,
        namespace: str,
        kube: CoreV1Api,
        nodes: list[str] | None = None,
        statuses: list[str] | None = None,
        name_prefixes: list[str] | None = None,
    ) -> PodListData:
        """List pods in *namespace*, filtered by node / status / name prefix.

        When ``namespace == "*"`` lists pods across all namespaces; otherwise
        scopes to the single namespace.

        Filter semantics: values within a parameter are OR'd; the three
        parameters are AND'd. An empty/None parameter does not filter that
        dimension. ``statuses`` matches pod phase, case-insensitive.
        ``name_prefixes`` is a prefix match.

        Raises:
            KubeApiException: On Kubernetes API failure.
        """
        try:
            if namespace == "*":
                pod_list = kube.list_pod_for_all_namespaces()
            else:
                pod_list = kube.list_namespaced_pod(namespace)
        except ApiException as exc:
            raise KubeApiException(
                f"Failed to list pods in namespace '{namespace}' "
                f"of cluster '{cluster}': {exc.reason}",
                kube_status=exc.status,
            ) from exc

        node_set = set(nodes) if nodes else None
        status_set = {s.lower() for s in statuses} if statuses else None
        prefixes = tuple(name_prefixes) if name_prefixes else None

        pods: list[PodInfo] = []
        for raw in pod_list.items:
            info = self._pod_to_info(raw)
            if node_set is not None and info.node_name not in node_set:
                continue
            if status_set is not None and info.phase.lower() not in status_set:
                continue
            if prefixes is not None and not info.name.startswith(prefixes):
                continue
            pods.append(info)

        _logger.info(
            "Listed %d pod(s) | cluster=%s | namespace=%s",
            len(pods), cluster, namespace,
        )
        return PodListData(cluster=cluster, namespace=namespace, pods=pods)
```

- [ ] **Step 6: Run the list_pods tests to verify they pass**

Run: `APP_ENV=test uv run pytest tests/unit/test_node_service.py -k list_pods -v`
Expected: All 10 PASS.

- [ ] **Step 7: Run the full node_service suite**

Run: `APP_ENV=test uv run pytest tests/unit/test_node_service.py -v`
Expected: All PASS.

- [ ] **Step 8: Commit**

```bash
git add app/services/node_service.py tests/unit/test_node_service.py
git commit -m "feat: add NodeService.list_pods with node/status/name filters"
```

---

### Task 4: pods.py router + comma-split helper

**Files:**
- Create: `app/api/v1/pods.py`
- Modify: `app/api/router.py`

- [ ] **Step 1: Create `app/api/v1/pods.py`**

```python
"""
app/api/v1/pods.py

Pod query endpoint (v1).

Route:
  GET /api/v1/clusters/{cluster}/pods → list pods in a namespace, filtered.

Requires the ``cluster_api`` scope.
"""

from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Request

from app.core.config import get_settings
from app.core.dependencies import get_current_user
from app.domain.kubernetes_models import PodListData
from app.domain.models import ApiResponse, User
from app.repositories.cluster_repository import ClusterRepository
from app.repositories.yaml_cluster_repository import YamlClusterRepository
from app.services.kube_client import KubeClientFactory
from app.services.node_service import NodeService

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/clusters", tags=["pods"])


def _get_cluster_repo() -> ClusterRepository:
    settings = get_settings()
    return YamlClusterRepository(settings.KUBECONFIG_BASE_PATH)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _split_csv(value: Optional[str]) -> list[str]:
    """Split a comma-separated query value into a trimmed, blank-free list."""
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@router.get(
    "/{cluster}/pods",
    response_model=ApiResponse[PodListData],
    summary="List pods in a namespace",
    description=(
        "Lists pods in the required ``namespace`` (use ``*`` for all "
        "namespaces), optionally filtered by ``node``, ``status`` (pod phase, "
        "case-insensitive), and ``pod_name`` (prefix match). Each filter "
        "accepts a comma-separated list; values within a filter are OR'd, "
        "filters are AND'd together."
    ),
)
async def list_pods(
    request: Request,
    cluster: str,
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))],
    namespace: str = Query(..., description="Namespace to list pods from (required; '*' = all)."),
    node: Optional[str] = Query(None, description="Comma-separated node names."),
    status: Optional[str] = Query(None, description="Comma-separated pod phases."),
    pod_name: Optional[str] = Query(None, description="Comma-separated name prefixes."),
    repo: ClusterRepository = Depends(_get_cluster_repo),
) -> ApiResponse[PodListData]:
    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    data = NodeService().list_pods(
        cluster=cluster,
        namespace=namespace,
        kube=kube,
        nodes=_split_csv(node),
        statuses=_split_csv(status),
        name_prefixes=_split_csv(pod_name),
    )
    return ApiResponse(data=data, request_id=_request_id(request))
```

- [ ] **Step 2: Mount the router in `app/api/router.py`**

Add the import alongside the others:

```python
from app.api.v1.pods import router as pods_router
```

And include it after the nodes router:

```python
v1_router.include_router(pods_router)    # mounts at /api/v1/clusters/{cluster}/pods
```

Also add this line to the route-layout docstring at the top, under the nodes lines:

```text
  GET  /api/v1/clusters/{cluster}/pods                         → List pods in a namespace (filtered)
```

- [ ] **Step 3: Verify the app boots and the route is registered**

Run: `APP_ENV=test uv run python -c "from app.main import create_app; app = create_app(); paths = [r.path for r in app.routes]; assert '/api/v1/clusters/{cluster}/pods' in paths, paths; print('ok')"`
Expected: prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add app/api/v1/pods.py app/api/router.py
git commit -m "feat: add GET /clusters/{cluster}/pods query endpoint"
```

---

### Task 5: Update node detail route docstring + clean up nodes.py imports

**Files:**
- Modify: `app/api/v1/nodes.py`

- [ ] **Step 1: Update the `get_node` route description (drop pods wording)**

In `app/api/v1/nodes.py`, change the `@router.get("/{cluster}/nodes/{node}", ...)` decorator's `description` to:

```python
    description=(
        "Returns full node information — status, roles, kubelet version, labels, "
        "annotations, schedulability. Pods are queried via "
        "GET /clusters/{cluster}/pods."
    ),
```

Also update the module docstring's route line at the top of the file:

```text
  GET   /api/v1/clusters/{cluster}/nodes/{node}             → get node detail (no pods)
```

- [ ] **Step 2: Verify the app still boots**

Run: `APP_ENV=test uv run python -c "from app.main import create_app; create_app(); print('ok')"`
Expected: prints `ok`.

- [ ] **Step 3: Commit**

```bash
git add app/api/v1/nodes.py
git commit -m "docs: update node detail route to reflect pods removal"
```

---

### Task 6: Full suite + final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `APP_ENV=test uv run pytest tests/ -v`
Expected: All PASS. If any integration test asserted on `NodeDetailData.pods`, fix that assertion to match the new schema (node detail no longer has pods).

- [ ] **Step 2: Confirm no lingering references to the removed pods field**

Run: `grep -rn "\.pods" app/ --include="*.py" | grep -iv "drained_pods\|pods_to_evict\|pod_list\|list_pod"`
Expected: only `PodListData.pods` / `NodeService.list_pods` related lines — no reference to `NodeDetailData(...).pods` or `data.pods` on a node-detail response.

- [ ] **Step 3: Final commit if any fixes were needed**

```bash
git add -A
git commit -m "test: align suite with slim node detail + pods query"
```

(Skip if Step 1 and 2 were already clean with nothing to commit.)

---

## Notes for the implementer

- `NodeService` is stateless for pod listing — `pods.py` instantiates it directly as `NodeService()`, matching how `clusters.py::list_nodes` already does it.
- `str.startswith` accepts a tuple of prefixes, which is exactly the OR semantics we want — no manual loop needed.
- The `node` query filter matches `spec.nodeName` exactly (set via `_pod_to_info`'s `node_name`), not a prefix.
- Do not add pagination or label-selector support — explicitly out of scope per the spec.
