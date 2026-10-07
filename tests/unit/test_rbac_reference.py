"""
tests/unit/test_rbac_reference.py

The RBAC reference (docs/rbac/cluster-service-clusterrole.yaml) must grant
exactly the Kubernetes permissions the service code uses — nothing missing,
nothing extra.

Nothing else catches drift: local k3d runs on an admin kubeconfig, so a missing
permission passes every local test and only surfaces as a 403 in production.
This test finds every call to a ``CoreV1Api`` method anywhere under ``app/``
(by AST, whatever the receiver is named; dry-run fakes excluded) and compares
the permissions they need against the ClusterRole's rules.

A new ``CoreV1Api`` method fails here until it is added to ``_PERMISSIONS``
*and* granted in the reference file — that is the point. Only ``CoreV1Api`` is
covered; a call through another API class (``AppsV1Api``, …) needs this test
extended first.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml
from kubernetes.client import CoreV1Api

_ROOT = Path(__file__).resolve().parents[2]
_RBAC_FILE = _ROOT / "docs" / "rbac" / "cluster-service-clusterrole.yaml"
_APP = _ROOT / "app"

# CoreV1Api method → (resource, verb) it needs. Explicit rather than derived
# from the method name: SDK names do not map mechanically (``read`` is the
# ``get`` verb, ``config_map`` is ``configmaps``, eviction is a subresource).
_PERMISSIONS: dict[str, tuple[str, str]] = {
    "list_node": ("nodes", "list"),
    "read_node": ("nodes", "get"),
    "patch_node": ("nodes", "patch"),
    "list_pod_for_all_namespaces": ("pods", "list"),
    "list_namespaced_pod": ("pods", "list"),
    "delete_namespaced_pod": ("pods", "delete"),
    "create_namespaced_pod_eviction": ("pods/eviction", "create"),
}

_CORE_V1_METHODS = {name for name in dir(CoreV1Api) if not name.startswith("_")}


def _methods_used() -> set[str]:
    """Every CoreV1Api method called from real (non-dry-run) app code."""
    methods: set[str] = set()
    for path in _APP.rglob("*.py"):
        if path.name.startswith("dry_run_"):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _CORE_V1_METHODS
            ):
                methods.add(node.func.attr)
    return methods


def _needed() -> set[tuple[str, str]]:
    return {_PERMISSIONS[m] for m in _methods_used() if m in _PERMISSIONS}


def _documents() -> dict[str, dict]:
    docs = [d for d in yaml.safe_load_all(_RBAC_FILE.read_text()) if d]
    by_kind = {d["kind"]: d for d in docs}
    assert len(by_kind) == len(docs), "each kind must appear exactly once"
    return by_kind


def _granted() -> set[tuple[str, str]]:
    granted: set[tuple[str, str]] = set()
    for rule in _documents()["ClusterRole"]["rules"]:
        assert rule.get("apiGroups") == [""], (
            "only core-group resources are used; a rule for another API group "
            "needs its own entry in this test"
        )
        for resource in rule["resources"]:
            for verb in rule["verbs"]:
                granted.add((resource, verb))
    return granted


def test_the_scan_finds_kubernetes_calls() -> None:
    # Guards the guard: if the scan stopped matching, every other assertion
    # here would pass vacuously.
    assert "patch_node" in _methods_used()


def test_every_kubernetes_call_has_a_known_permission() -> None:
    unknown = _methods_used() - _PERMISSIONS.keys()
    assert not unknown, (
        f"CoreV1Api methods with no permission mapping: {sorted(unknown)}. "
        "Add them to _PERMISSIONS and grant them in the RBAC reference."
    )


def test_the_reference_grants_every_permission_the_code_needs() -> None:
    missing = _needed() - _granted()
    assert not missing, f"used in code but not granted: {sorted(missing)}"


def test_the_reference_grants_nothing_the_code_does_not_use() -> None:
    extra = _granted() - _needed()
    assert not extra, f"granted but never used: {sorted(extra)}"


def test_the_role_is_bound_to_the_service_account() -> None:
    docs = _documents()
    sa = docs["ServiceAccount"]
    binding = docs["ClusterRoleBinding"]

    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": docs["ClusterRole"]["metadata"]["name"],
    }
    assert {
        "kind": "ServiceAccount",
        "name": sa["metadata"]["name"],
        "namespace": sa["metadata"]["namespace"],
    } in binding["subjects"]
