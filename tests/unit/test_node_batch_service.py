"""
tests/unit/test_node_batch_service.py

NodeBatchService: the policy is judged per node before the batch runs, and a
batch the policy refused entirely never resolves the cluster.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.domain.access_policy import AccessPolicies, PolicyRule
from app.domain.models import User
from app.repositories.policy_repository import PolicyRepository
from app.services import node_batch_service
from app.services.node_batch_service import NodeBatchService
from app.services.node_service import NodeService
from app.services.policy_service import PolicyService

_USER = User(account="alice", scopes=["cluster_api"])


class _Repo(PolicyRepository):
    def __init__(self, *rules):
        self.rules = [PolicyRule.model_validate(r) for r in rules]

    async def get_policies(self, account):
        return AccessPolicies(account, self.rules, [])


def _deny(node_regex: str, action: str = "node.*") -> dict:
    return {"id": f"deny-{node_regex}", "actions": [action], "effect": "deny", "match": {"node": node_regex}}


class _RecordingNodes(NodeService):
    def __init__(self):
        self.calls = []

    def cordon_many(self, cluster, node_names, kube, denied=None):
        self.calls.append(("cordon", node_names, dict(denied or {})))
        return super().cordon_many(cluster, node_names, kube, denied)

    def uncordon_many(self, cluster, node_names, kube, denied=None):
        self.calls.append(("uncordon", node_names, dict(denied or {})))
        return super().uncordon_many(cluster, node_names, kube, denied)


@pytest.fixture
def kube_calls(monkeypatch):
    """Stand in for call_kube: record that the cluster was resolved."""
    calls = []

    async def fake_call_kube(repo, cluster, op):
        calls.append(cluster)
        return op(MagicMock())

    monkeypatch.setattr(node_batch_service, "call_kube", fake_call_kube)
    return calls


def _svc(nodes, *rules) -> NodeBatchService:
    return NodeBatchService(nodes, PolicyService(_Repo(*rules)))


async def test_denied_nodes_reach_node_service_as_denied(kube_calls):
    nodes = _RecordingNodes()
    data = await _svc(nodes, _deny("cp-.*")).run(
        user=_USER, repo=None, cluster="c1", action="cordon",
        node_names=["w-1", "cp-0", "w-1"],
    )
    assert kube_calls == ["c1"]
    action, names, denied = nodes.calls[0]
    assert (action, names, list(denied)) == ("cordon", ["w-1", "cp-0"], ["cp-0"])
    assert [(r.node, r.status) for r in data.results] == [("w-1", "success"), ("cp-0", "denied")]


async def test_policy_is_judged_under_the_matching_action(kube_calls):
    """A rule on node.uncordon must not refuse a cordon, and vice versa."""
    nodes = _RecordingNodes()
    svc = _svc(nodes, _deny(".*", action="node.uncordon"))

    cordon = await svc.run(user=_USER, repo=None, cluster="c", action="cordon", node_names=["n"])
    uncordon = await svc.run(user=_USER, repo=None, cluster="c", action="uncordon", node_names=["n"])

    assert cordon.summary.denied == 0
    assert uncordon.summary.denied == 1


async def test_a_fully_denied_batch_never_resolves_the_cluster(kube_calls):
    nodes = _RecordingNodes()
    data = await _svc(nodes, _deny(".*")).run(
        user=_USER, repo=None, cluster="prod", action="uncordon", node_names=["a", "b"],
    )
    assert kube_calls == []
    assert nodes.calls == []
    assert data.action == "uncordon"
    assert data.summary.model_dump() == {"total": 2, "succeeded": 0, "failed": 0, "denied": 2}


async def test_an_empty_batch_still_resolves_the_cluster(kube_calls):
    """Unchanged from before the policy: an unknown cluster stays a 404."""
    await _svc(_RecordingNodes()).run(
        user=_USER, repo=None, cluster="c", action="cordon", node_names=[],
    )
    assert kube_calls == ["c"]
