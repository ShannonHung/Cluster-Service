"""
tests/unit/test_policy_service.py

PolicyService: enforce for single-resource writes, per-node verdicts for
batches.
"""

from __future__ import annotations

import pytest

from app.core.exceptions import PolicyDeniedException
from app.domain.access_policy import AccessPolicies, PolicyAction, PolicyParam, PolicyRule
from app.domain.models import User
from app.repositories.policy_repository import PolicyRepository
from app.services.policy_service import PolicyService

_ALICE = User(account="alice", scopes=["cluster_api"])


class _Repo(PolicyRepository):
    def __init__(self, global_rules=(), user_rules=()):
        self.global_rules = [PolicyRule.model_validate(r) for r in global_rules]
        self.user_rules = [PolicyRule.model_validate(r) for r in user_rules]
        self.calls = 0

    async def get_policies(self, account):
        self.calls += 1
        return AccessPolicies(account, self.global_rules, self.user_rules)


_NO_MASTER = {
    "id": "no-master", "actions": ["node.*"], "effect": "deny",
    "match": {"node": ".*-master-\\d+"}, "description": "control plane",
}


async def test_enforce_passes_when_allowed():
    await PolicyService(_Repo()).enforce(
        _ALICE, PolicyAction.NODE_DRAIN, {PolicyParam.CLUSTER: "c", PolicyParam.NODE: "w-1"},
    )


async def test_enforce_raises_403_with_the_rule_in_detail():
    svc = PolicyService(_Repo([_NO_MASTER]))
    with pytest.raises(PolicyDeniedException) as info:
        await svc.enforce(
            _ALICE, PolicyAction.NODE_DRAIN,
            {PolicyParam.CLUSTER: "c", PolicyParam.NODE: "cp-master-0"},
        )

    exc = info.value
    assert exc.http_status == 403
    assert exc.error_code == "POLICY_DENIED"
    assert "control plane" in exc.message
    assert exc.detail == {
        "action": "node.drain",
        "layer": "global",
        "rule_id": "no-master",
        "params": {"cluster": "c", "node": "cp-master-0"},
    }


async def test_denied_nodes_loads_the_policy_once_per_batch():
    repo = _Repo([_NO_MASTER])
    denied = await PolicyService(repo).denied_nodes(
        _ALICE, PolicyAction.NODE_CORDON, cluster="c", nodes=["w-1", "cp-master-0", "cp-master-1"],
    )
    assert repo.calls == 1
    assert set(denied) == {"cp-master-0", "cp-master-1"}
    assert "control plane" in denied["cp-master-0"]
