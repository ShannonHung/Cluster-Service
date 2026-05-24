# Pods Query API + Slim Node Detail — Design

Date: 2026-05-24
Status: Approved

## Problem

`GET /api/v1/clusters/{cluster}/nodes/{node}` currently returns full node
detail **plus** every pod assigned to the node. Bundling pods into the node
response makes it heavy and conflates two concerns. We want:

1. A dedicated, filterable pods query endpoint.
2. Node detail stripped of pods.

## Endpoint

```
GET /api/v1/clusters/{cluster}/pods
    ?namespace=foo            (required)
    &node=node-1,node-2       (optional, comma-separated, OR)
    &status=Running,Pending   (optional, comma-separated, OR, matches pod phase)
    &pod_name=web-,api-       (optional, comma-separated, prefix match, OR)
```

- Scope: `cluster_api`.
- Returns `ApiResponse[PodListData]`.
- Filter semantics: values **within** one parameter are OR'd; the three
  parameters are AND'd together. Empty parameter = no filtering on that
  dimension.
- `status` matches the pod `phase` (Running / Pending / Succeeded / Failed /
  Unknown), case-insensitive.
- `pod_name` is a **prefix** match: `web-` matches `web-7d9f-abc`.
- `node` matches the pod's assigned node (`spec.nodeName`) exactly.

## Data flow (reuses existing layering)

```
pods.py router
  → repo.get_kube_client_config(cluster)
  → KubeClientFactory().get_core_v1(cfg)
  → NodeService.list_pods(cluster, namespace, kube, nodes, statuses, name_prefixes)
```

`list_pods` calls `kube.list_namespaced_pod(namespace)` (lighter than
`list_pod_for_all_namespaces`) and applies node / status / pod_name filters in
Python. Required namespace keeps the query naturally bounded.

`list_pods` lives on the existing `NodeService` (reuses `_pod_to_info`); no new
service class.

## Model changes (`app/domain/kubernetes_models.py`)

- `PodInfo`: add `node_name: str` field (pods query spans nodes, so the caller
  needs to know where each pod runs).
- New `PodListData { cluster: str, namespace: str, pods: list[PodInfo] }`.
- `NodeDetailData`: **remove** the `pods` field.

## Node detail slim-down

- `NodeService.get_node()` no longer calls `list_pod_for_all_namespaces`; it
  returns node attributes only.
- Remove pods references from `nodes.py` route docstrings.
- This is a **breaking change** to the node-detail response schema.

## Error handling

- Non-existent namespace → Kubernetes returns an empty list → respond with
  `pods: []` (no error).
- Other Kubernetes API failures → existing `KubeApiException` path.

## Filtering detail

- Comma-split helper turns `a,b` into `["a", "b"]`, trimming blanks.
- A pod passes when: (no node filter OR its node in nodes) AND (no status
  filter OR its phase in statuses) AND (no name filter OR its name starts with
  any prefix).

## Testing

- `tests/unit/test_node_service.py`: add `list_pods` unit tests with a fake
  `CoreV1Api`, covering each filter, combinations, and empty results.
- Update existing `get_node` tests to drop pods assertions.
- Mount the new router in `app/api/router.py`; update its route-layout
  docstring.

## Out of scope

- Pagination, label-selector filtering, cross-namespace queries.
