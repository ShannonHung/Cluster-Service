# Node Taint API — Design

Date: 2026-05-24
Status: Approved

## Problem

The service supports cordon / uncordon / drain / label / annotate node operations
but not taints. Taints (`kubectl taint`) are a common node-management primitive
for steering pod scheduling. Add a taint endpoint following the existing node
operation patterns.

## Endpoint

```text
PATCH /api/v1/clusters/{cluster}/nodes/{node}/taints
```

- Scope: `cluster_api`.
- Request body:

```json
{
  "set":    [{"key": "gpu", "value": "true", "effect": "NoSchedule"}],
  "remove": [{"key": "gpu", "effect": "NoSchedule"}]
}
```

- Returns `ApiResponse[NodeTaintData]` with the node's **current** taints after
  the patch (mirrors how `label_node` / `annotate_node` return current state).

## Semantics

- A Kubernetes node taint is uniquely identified by `(key, effect)`. A node
  cannot hold two taints with the same key+effect.
- `set`: insert the taint, or **overwrite** `value` if a taint with the same
  `(key, effect)` already exists (matches `kubectl taint --overwrite`).
- `remove`: drop the taint matching `(key, effect)`. Removing a taint that does
  not exist is a no-op (not an error).
- `effect` is restricted to `NoSchedule | PreferNoSchedule | NoExecute`
  (validated at the request layer via `Literal`; invalid → 422).
- Empty `set` and empty `remove` → no patch sent; the endpoint just reads and
  returns the current taints.
- Order of application: apply `remove` first, then `set` (so a set with the same
  key+effect as a remove wins).

## Models (`app/domain/kubernetes_models.py`)

```python
class TaintSpec(BaseModel):
    key: str
    value: Optional[str] = None
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"]

class TaintRemoveSpec(BaseModel):
    key: str
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"]

class NodeTaintRequest(BaseModel):
    set: list[TaintSpec] = Field(default_factory=list)
    remove: list[TaintRemoveSpec] = Field(default_factory=list)

class NodeTaintData(BaseModel):
    status: str = "success"
    cluster: str
    node: str
    action: str = "taint"
    taints: list[TaintSpec] = Field(default_factory=list)
```

## Service: `NodeService.taint_node`

`spec.taints` is a **list**, not a map — Kubernetes has no merge-by-key for it,
so the service recomputes the whole list and patches it wholesale:

1. `read_node` → current `spec.taints` (404 → `NodeNotFoundException`).
2. Build an ordered dict keyed by `(key, effect)` from the current taints.
3. Apply `remove`: drop entries matching `(key, effect)`.
4. Apply `set`: insert or overwrite by `(key, effect)` (overwrites `value`).
5. If `set` and `remove` are both empty → skip the patch; read and return
   current taints as `list[TaintSpec]`.
6. Otherwise `patch_node` with the full recomputed `spec.taints` list, then
   re-read and return the current taints.

A private helper `_to_taint_spec(v1_taint) -> TaintSpec` converts a `V1Taint`.
Patch failures (non-404) → `KubeApiException`.

## Router (`app/api/v1/nodes.py`)

New `PATCH .../taints` handler mirroring `patch_node_labels`: accept
`NodeTaintRequest` body, resolve `repo → KubeClientFactory → NodeService`, wrap
the result in `ApiResponse`. Update the module docstring route list and the
`app/api/router.py` route-layout docstring.

## Error handling

- Invalid `effect` → 422 (Pydantic, before service).
- Node not found → `NodeNotFoundException` (404).
- Other Kubernetes API failures → `KubeApiException`.

## Testing (`tests/unit/test_node_service.py`)

Extend `_make_node` to carry `spec.taints` (default empty). New `taint_node`
tests:

- set adds a new taint.
- remove drops a taint by key+effect.
- set with same key+effect overwrites value.
- set + remove together.
- remove of a non-existent taint is a no-op (no error).
- empty set + empty remove → no patch_node call, returns current taints.
- 404 on read → `NodeNotFoundException`.
- response `taints` reflects current node state after patch.

## rest_client

Add a Taints section to `rest_client/cluster.http` with set / remove / combined
examples and a 422 invalid-effect example.

## Out of scope

- No interaction with drain/eviction logic. Removing a `NoExecute` taint relies
  on normal Kubernetes rescheduling behaviour; the API does nothing special.
- No taint listing endpoint (taints are returned by the node-detail read and by
  this endpoint's response).
