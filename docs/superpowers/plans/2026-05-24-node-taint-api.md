# Node Taint API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `PATCH /api/v1/clusters/{cluster}/nodes/{node}/taints` to set/remove node taints, following the existing label/annotation patterns.

**Architecture:** New request/response models in `kubernetes_models.py`; a `NodeService.taint_node` method that reads current `spec.taints`, recomputes the list (remove then set, keyed by `(key, effect)`), patches the whole list, and returns current taints; a new router handler in `nodes.py` mirroring `patch_node_labels`.

**Tech Stack:** FastAPI, Pydantic (`Literal` for effect validation), kubernetes Python SDK, pytest (asyncio_mode=auto).

**Run all commands from `cluster-service/`. Tests use `APP_ENV=test`.** uv may need network for its cache; if a command fails on cache permissions that is a sandbox issue, not a code issue.

---

### Task 1: Taint models

**Files:**
- Modify: `app/domain/kubernetes_models.py`

- [ ] **Step 1: Confirm `Literal` is imported**

`app/domain/kubernetes_models.py` already imports `from typing import Literal, Optional` (used by `KubeClientConfig`). No new import needed. `Field`, `BaseModel` are also already imported.

- [ ] **Step 2: Add taint models**

In `app/domain/kubernetes_models.py`, directly AFTER the `NodeMetadataData` class (the class with `action: str  # "label" | "annotate"`), add:

```python
class TaintSpec(BaseModel):
    """A single node taint (set form). ``(key, effect)`` is the unique key."""

    key: str
    value: Optional[str] = None
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"]


class TaintRemoveSpec(BaseModel):
    """Identifies a taint to remove by its unique ``(key, effect)``."""

    key: str
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"]


class NodeTaintRequest(BaseModel):
    """HTTP request body for PATCH …/taints."""

    set: list[TaintSpec] = Field(default_factory=list, description="Taints to add or overwrite.")
    remove: list[TaintRemoveSpec] = Field(default_factory=list, description="Taints to remove by key+effect.")


class NodeTaintData(BaseModel):
    """Response for PATCH …/taints — the node's current taints after the patch."""

    status: str = "success"
    cluster: str
    node: str
    action: str = "taint"
    taints: list[TaintSpec] = Field(default_factory=list)
```

- [ ] **Step 3: Verify import + validation**

Run: `APP_ENV=test uv run python -c "import app.domain.kubernetes_models as m; t=m.TaintSpec(key='gpu', effect='NoSchedule'); assert t.value is None; r=m.NodeTaintRequest(); assert r.set==[] and r.remove==[]; print('ok')"`
Expected: `ok`

Run (invalid effect must raise): `APP_ENV=test uv run python -c "import app.domain.kubernetes_models as m; \nfrom pydantic import ValidationError; \ntry:\n    m.TaintSpec(key='x', effect='Nope'); print('FAIL: no error')\nexcept ValidationError:\n    print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add app/domain/kubernetes_models.py
git commit -m "feat: add node taint request/response models"
```

---

### Task 2: NodeService.taint_node

**Files:**
- Modify: `app/services/node_service.py`
- Modify: `tests/unit/test_node_service.py`

- [ ] **Step 1: Extend the `_make_node` test helper to carry taints**

In `tests/unit/test_node_service.py`, the `_make_node` helper (starts at line 32) sets `node.spec.unschedulable`. Add a `taints` parameter and set `node.spec.taints`. Change the signature and body so it accepts `taints: list | None = None` and adds, after the `node.spec.unschedulable = unschedulable` line:

```python
    node.spec.taints = taints if taints is not None else []
```

And add `taints: list | None = None,` as a parameter in the `_make_node(...)` signature (alongside the existing `labels`, `annotations`, etc. params).

Also add a small helper right after `_make_node` to build a fake V1Taint-shaped object:

```python
def _make_taint(key: str, effect: str, value: str | None = None) -> MagicMock:
    t = MagicMock()
    t.key = key
    t.value = value
    t.effect = effect
    return t
```

- [ ] **Step 2: Write failing tests**

Add `NodeTaintData` and `TaintSpec` to the import from `app.domain.kubernetes_models` at the top of the test file. Then add this new section at the end of the file:

```python
# ── taint_node ────────────────────────────────────────────────────────────────
# Pattern: read_node is called twice when a patch happens — once to compute the
# new list, once after the patch to return current state. So set side_effect to
# [node_before, node_after]. For the no-op case (empty set+remove), read_node is
# called once → use return_value.

def test_taint_node_adds_new_taint():
    kube = _make_kube()
    # read #1 (compute): no taints. read #2 (return current): the new taint.
    kube.read_node.side_effect = [
        _make_node("n", taints=[]),
        _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "true")]),
    ]
    from app.domain.kubernetes_models import TaintSpec
    result = _svc().taint_node(
        "test", "n", kube,
        set_taints=[TaintSpec(key="gpu", value="true", effect="NoSchedule")],
        remove_taints=[],
    )
    # A patch was sent with the new taint in spec.taints.
    body = kube.patch_node.call_args[0][1]
    taints = body["spec"]["taints"]
    assert {(t["key"], t["effect"], t.get("value")) for t in taints} == {("gpu", "NoSchedule", "true")}
    assert result.action == "taint"
    assert any(t.key == "gpu" and t.effect == "NoSchedule" for t in result.taints)


def test_taint_node_removes_by_key_and_effect():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "true")]),
        _make_node("n", taints=[]),
    ]
    from app.domain.kubernetes_models import TaintRemoveSpec
    result = _svc().taint_node(
        "test", "n", kube,
        set_taints=[],
        remove_taints=[TaintRemoveSpec(key="gpu", effect="NoSchedule")],
    )
    body = kube.patch_node.call_args[0][1]
    assert body["spec"]["taints"] == []
    assert result.taints == []


def test_taint_node_same_key_effect_overwrites_value():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "old")]),
        _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "new")]),
    ]
    from app.domain.kubernetes_models import TaintSpec
    _svc().taint_node(
        "test", "n", kube,
        set_taints=[TaintSpec(key="gpu", value="new", effect="NoSchedule")],
        remove_taints=[],
    )
    body = kube.patch_node.call_args[0][1]
    taints = body["spec"]["taints"]
    # Exactly one gpu/NoSchedule taint, value overwritten.
    matching = [t for t in taints if t["key"] == "gpu" and t["effect"] == "NoSchedule"]
    assert len(matching) == 1
    assert matching[0]["value"] == "new"


def test_taint_node_set_and_remove_together():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[_make_taint("old", "NoSchedule", None)]),
        _make_node("n", taints=[_make_taint("new", "NoExecute", "1")]),
    ]
    from app.domain.kubernetes_models import TaintSpec, TaintRemoveSpec
    _svc().taint_node(
        "test", "n", kube,
        set_taints=[TaintSpec(key="new", value="1", effect="NoExecute")],
        remove_taints=[TaintRemoveSpec(key="old", effect="NoSchedule")],
    )
    body = kube.patch_node.call_args[0][1]
    keys = {(t["key"], t["effect"]) for t in body["spec"]["taints"]}
    assert keys == {("new", "NoExecute")}


def test_taint_node_remove_nonexistent_is_noop_not_error():
    kube = _make_kube()
    kube.read_node.side_effect = [
        _make_node("n", taints=[_make_taint("keep", "NoSchedule", None)]),
        _make_node("n", taints=[_make_taint("keep", "NoSchedule", None)]),
    ]
    from app.domain.kubernetes_models import TaintRemoveSpec
    result = _svc().taint_node(
        "test", "n", kube,
        set_taints=[],
        remove_taints=[TaintRemoveSpec(key="ghost", effect="NoExecute")],
    )
    body = kube.patch_node.call_args[0][1]
    keys = {(t["key"], t["effect"]) for t in body["spec"]["taints"]}
    assert keys == {("keep", "NoSchedule")}
    assert any(t.key == "keep" for t in result.taints)


def test_taint_node_no_op_when_nothing_provided():
    kube = _make_kube()
    kube.read_node.return_value = _make_node("n", taints=[_make_taint("gpu", "NoSchedule", "true")])
    result = _svc().taint_node("test", "n", kube, set_taints=[], remove_taints=[])
    kube.patch_node.assert_not_called()
    assert any(t.key == "gpu" for t in result.taints)


def test_taint_node_raises_node_not_found_on_404():
    kube = _make_kube()
    kube.read_node.side_effect = _api_error(404)
    from app.domain.kubernetes_models import TaintSpec
    with pytest.raises(NodeNotFoundException):
        _svc().taint_node(
            "test", "missing", kube,
            set_taints=[TaintSpec(key="gpu", effect="NoSchedule")],
            remove_taints=[],
        )
```

- [ ] **Step 3: Run to verify they fail**

Run: `APP_ENV=test uv run pytest tests/unit/test_node_service.py -k taint_node -v`
Expected: FAIL — `taint_node` does not exist (AttributeError). 7 tests collected.

- [ ] **Step 4: Add models to the service import**

In `app/services/node_service.py`, the import block `from app.domain.kubernetes_models import (...)` (lines 33-44) lists models alphabetically-ish. Add `NodeTaintData`, `TaintSpec` to it. The block becomes:

```python
from app.domain.kubernetes_models import (
    DrainActionData,
    DrainOptions,
    DrainedPodInfo,
    NodeActionData,
    NodeDetailData,
    NodeInfo,
    NodeListData,
    NodeMetadataData,
    NodeTaintData,
    PodInfo,
    PodListData,
    TaintSpec,
)
```

- [ ] **Step 5: Implement `taint_node` + `_to_taint_spec`**

In `app/services/node_service.py`, add the `taint_node` method right after the `annotate_node` method (before the `# ── Private helpers ──` section). Use TYPE_CHECKING-free signature with the request models:

```python
    def taint_node(
        self,
        cluster: str,
        node_name: str,
        kube: CoreV1Api,
        set_taints: list[TaintSpec] | None = None,
        remove_taints: list | None = None,
    ) -> NodeTaintData:
        """Set / remove taints on *node_name*; return current taints.

        ``spec.taints`` is a list, not a map, so we read the current taints,
        recompute the full list (remove first, then set — keyed by
        ``(key, effect)`` so a set overwrites an existing taint's value), and
        patch the whole list. Empty set+remove skips the patch.

        Raises:
            NodeNotFoundException: Node does not exist.
            KubeApiException: On Kubernetes API failure.
        """
        set_taints = set_taints or []
        remove_taints = remove_taints or []

        current = self._read_node(cluster, node_name, kube)
        existing = current.spec.taints or []

        # key by (key, effect) preserving insertion order
        by_id: dict[tuple[str, str], dict] = {}
        for t in existing:
            by_id[(t.key, t.effect)] = {"key": t.key, "value": t.value, "effect": t.effect}

        if set_taints or remove_taints:
            for r in remove_taints:
                by_id.pop((r.key, r.effect), None)
            for s in set_taints:
                by_id[(s.key, s.effect)] = {"key": s.key, "value": s.value, "effect": s.effect}

            new_taints = list(by_id.values())
            try:
                kube.patch_node(node_name, {"spec": {"taints": new_taints}})
            except ApiException as exc:
                if exc.status == 404:
                    raise NodeNotFoundException(
                        f"Node '{node_name}' not found in cluster '{cluster}'.",
                    ) from exc
                raise KubeApiException(
                    f"Failed to patch taints on node '{node_name}': {exc.reason}",
                    kube_status=exc.status,
                ) from exc
            current = self._read_node(cluster, node_name, kube)

        taints = [self._to_taint_spec(t) for t in (current.spec.taints or [])]
        _logger.info("Patched taints | cluster=%s | node=%s", cluster, node_name)
        return NodeTaintData(cluster=cluster, node=node_name, taints=taints)
```

Then add these two private helpers in the `# ── Private helpers ──` section (after `_fetch_node_metadata`):

```python
    def _read_node(self, cluster: str, node_name: str, kube: CoreV1Api):
        """Read a node, mapping 404 → NodeNotFoundException."""
        try:
            return kube.read_node(node_name)
        except ApiException as exc:
            if exc.status == 404:
                raise NodeNotFoundException(
                    f"Node '{node_name}' not found in cluster '{cluster}'.",
                ) from exc
            raise KubeApiException(
                f"Failed to read node '{node_name}': {exc.reason}",
                kube_status=exc.status,
            ) from exc

    @staticmethod
    def _to_taint_spec(taint) -> TaintSpec:
        """Convert a V1Taint (or fake) to a TaintSpec."""
        return TaintSpec(key=taint.key, value=taint.value, effect=taint.effect)
```

- [ ] **Step 6: Run taint tests to verify they pass**

Run: `APP_ENV=test uv run pytest tests/unit/test_node_service.py -k taint_node -v`
Expected: All 7 PASS.

- [ ] **Step 7: Run the full node_service suite**

Run: `APP_ENV=test uv run pytest tests/unit/test_node_service.py -v`
Expected: All PASS. (The `_make_node` taints addition is additive; existing tests using `_make_node` must still pass.)

- [ ] **Step 8: Commit**

```bash
git add app/services/node_service.py tests/unit/test_node_service.py
git commit -m "feat: add NodeService.taint_node with set/remove semantics"
```

---

### Task 3: Taint router handler

**Files:**
- Modify: `app/api/v1/nodes.py`
- Modify: `app/api/router.py`

- [ ] **Step 1: Import the new request/response models in nodes.py**

In `app/api/v1/nodes.py`, the import block `from app.domain.kubernetes_models import (...)` lists models. Add `NodeTaintData` and `NodeTaintRequest` to it (keep the block sorted as it currently is).

- [ ] **Step 2: Add the route handler**

In `app/api/v1/nodes.py`, after the `patch_node_annotations` handler (the last route in the file), add:

```python
# ── PATCH …/taints ────────────────────────────────────────────────────────────

@router.patch(
    "/{cluster}/nodes/{node}/taints",
    response_model=ApiResponse[NodeTaintData],
    summary="Set or remove node taints",
    description=(
        "Set ``set`` to add/overwrite taints (a taint with the same key+effect "
        "overwrites the value), ``remove`` to delete taints by key+effect. "
        "``effect`` must be NoSchedule, PreferNoSchedule, or NoExecute. "
        "Response contains the node's **current** taints after the patch."
    ),
)
async def patch_node_taints(
    request: Request,
    cluster: str,
    node: str,
    body: NodeTaintRequest = NodeTaintRequest(),
    current_user: Annotated[User, Depends(get_current_user(["cluster_api"]))] = None,
    repo: ClusterRepository = Depends(_get_cluster_repo),
    svc: NodeService = Depends(_get_node_service),
) -> ApiResponse[NodeTaintData]:
    cfg = repo.get_kube_client_config(cluster)
    kube = KubeClientFactory().get_core_v1(cfg)
    data = svc.taint_node(
        cluster=cluster,
        node_name=node,
        kube=kube,
        set_taints=body.set,
        remove_taints=body.remove,
    )
    return ApiResponse(data=data, request_id=_request_id(request))
```

- [ ] **Step 3: Update the nodes.py module docstring**

In the module docstring at the top of `app/api/v1/nodes.py`, add this line to the Routes list (after the annotations line):

```text
  PATCH /api/v1/clusters/{cluster}/nodes/{node}/taints      → set/remove taints
```

- [ ] **Step 4: Update the router.py route-layout docstring**

In `app/api/router.py`, add this line to the docstring route list near the other node routes:

```text
  PATCH /api/v1/clusters/{cluster}/nodes/{node}/taints         → Set or remove node taints
```

(This is docstring-only; the nodes router is already mounted, so no `include_router` change is needed.)

- [ ] **Step 5: Verify the app boots and the route registers**

Run: `APP_ENV=test uv run python -c "from app.main import create_app; app = create_app(); paths = [r.path for r in app.routes]; assert '/api/v1/clusters/{cluster}/nodes/{node}/taints' in paths, paths; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Run the full suite**

Run: `APP_ENV=test uv run pytest tests/ -q`
Expected: All PASS.

- [ ] **Step 7: Commit**

```bash
git add app/api/v1/nodes.py app/api/router.py
git commit -m "feat: add PATCH /nodes/{node}/taints endpoint"
```

---

### Task 4: rest_client examples

**Files:**
- Modify: `rest_client/cluster.http`

- [ ] **Step 1: Add a Taints section**

In `rest_client/cluster.http`, after the Annotations section (before the "Error cases" section), add:

```text
# ══════════════════════════════════════════════════════
# Taints
# ══════════════════════════════════════════════════════

### Add / overwrite a taint
PATCH {{baseUrl}}/api/v1/clusters/{{cluster}}/nodes/{{agent1}}/taints
Authorization: Bearer {{token}}
Content-Type: application/json

{
    "set": [
        { "key": "gpu", "value": "true", "effect": "NoSchedule" }
    ],
    "remove": []
}

### Remove a taint (by key + effect)
PATCH {{baseUrl}}/api/v1/clusters/{{cluster}}/nodes/{{agent1}}/taints
Authorization: Bearer {{token}}
Content-Type: application/json

{
    "set": [],
    "remove": [
        { "key": "gpu", "effect": "NoSchedule" }
    ]
}

### Set and remove taints in one request
PATCH {{baseUrl}}/api/v1/clusters/{{cluster}}/nodes/{{agent1}}/taints
Authorization: Bearer {{token}}
Content-Type: application/json

{
    "set": [
        { "key": "dedicated", "value": "infra", "effect": "NoExecute" }
    ],
    "remove": [
        { "key": "gpu", "effect": "NoSchedule" }
    ]
}
```

- [ ] **Step 2: Add a 422 invalid-effect example in the Error cases section**

In the "Error cases (for testing)" section of `rest_client/cluster.http`, after the existing 422 empty-namespace example, add:

```text
### 422 — invalid taint effect
PATCH {{baseUrl}}/api/v1/clusters/{{cluster}}/nodes/{{agent1}}/taints
Authorization: Bearer {{token}}
Content-Type: application/json

{
    "set": [ { "key": "gpu", "effect": "BadEffect" } ],
    "remove": []
}
```

- [ ] **Step 3: Commit**

```bash
git add rest_client/cluster.http
git commit -m "docs: add taint examples to cluster.http"
```

NOTE: `rest_client/cluster.http` may already have uncommitted edits (a `pod_name=core` example). Only stage `rest_client/cluster.http` itself; do not stage other unrelated modified files (`pipeline_models.py`, `pyproject.toml`, etc.). The pre-existing edit to this file will be included in this commit — that is acceptable since it is a doc-only file.

---

### Task 5: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `APP_ENV=test uv run pytest tests/ -v`
Expected: All PASS.

- [ ] **Step 2: Confirm the route is registered and effect validation works**

Run: `APP_ENV=test uv run python -c "from app.main import create_app; app=create_app(); assert '/api/v1/clusters/{cluster}/nodes/{node}/taints' in [r.path for r in app.routes]; print('route ok')"`
Expected: `route ok`

- [ ] **Step 3: Final commit if anything was left**

If steps 1-2 surfaced fixes, commit them. Otherwise nothing to do.

---

## Notes for the implementer

- `spec.taints` is a **list**, not a map — that's why `taint_node` recomputes and patches the whole list rather than using the null-value deletion trick that labels use.
- The patch body uses plain dicts (`{"key", "value", "effect"}`), matching how labels patch with raw dicts; the kubernetes client serializes them fine.
- Effect validation is entirely at the Pydantic layer (`Literal`), so the service never needs to validate effect strings.
- Follow the existing `patch_node_labels` handler shape exactly — same dependency injection, same `ApiResponse` envelope.
- Do not add a taint-listing endpoint or any drain/eviction coupling — out of scope per the spec.
