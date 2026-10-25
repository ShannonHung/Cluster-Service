"""
tests/unit/test_access_policy.py

The request-parameter access policy: how rules in USERS_JSON_PATH are parsed
and how a request is judged against them. Pure domain logic — no file, no app.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.access_policy import (
    POLICY_ACTIONS,
    AccessPolicies,
    PolicyAction,
    PolicyParam,
    PolicyRule,
    evaluate,
)


def _rule(**kw) -> PolicyRule:
    base = {"id": "r", "actions": ["node.drain"], "effect": "deny", "match": {"cluster": "prod-.*"}}
    base.update(kw)
    return PolicyRule.model_validate(base)


def _policies(global_rules=(), user_rules=(), account="alice") -> AccessPolicies:
    return AccessPolicies(
        account=account,
        global_rules=list(global_rules),
        user_rules=list(user_rules),
    )


_PROD = {PolicyParam.CLUSTER: "prod-1", PolicyParam.NODE: "worker-1"}
_DEV = {PolicyParam.CLUSTER: "dev-1", PolicyParam.NODE: "worker-1"}


# ── parsing ───────────────────────────────────────────────────────────────────


def test_invalid_regex_is_refused_at_load():
    with pytest.raises(ValidationError, match="regex"):
        _rule(match={"cluster": "prod-("})


def test_unknown_action_is_refused_at_load():
    """A typo in an action would otherwise make the rule silently inert."""
    with pytest.raises(ValidationError, match="node.drian"):
        _rule(actions=["node.drian"])


def test_wildcard_action_must_match_a_known_action():
    with pytest.raises(ValidationError, match="pods"):
        _rule(actions=["pods.*"])


def test_unknown_match_key_is_refused_at_load():
    """`nodes` instead of `node` would make a deny rule never fire."""
    with pytest.raises(ValidationError, match="nodes"):
        _rule(match={"nodes": "master-.*"})


def test_match_key_must_belong_to_the_targeted_actions():
    with pytest.raises(ValidationError, match="namespace"):
        _rule(actions=["node.drain"], match={"namespace": "kube-.*"})


def test_empty_match_is_refused():
    """An empty match would be a catch-all — that must be spelled `.*`."""
    with pytest.raises(ValidationError):
        _rule(match={})


def test_extra_fields_are_refused():
    with pytest.raises(ValidationError):
        _rule(efect="deny")


def test_wildcard_expands_to_known_actions():
    rule = _rule(actions=["node.*"], match={"cluster": ".*"})
    assert rule.applies_to(PolicyAction.NODE_DRAIN)
    assert rule.applies_to(PolicyAction.NODE_CORDON)
    assert not rule.applies_to(PolicyAction.NAMESPACE_CREATE)


# ── deny ──────────────────────────────────────────────────────────────────────


def test_no_rules_allows():
    assert evaluate(_policies(), PolicyAction.NODE_DRAIN, _PROD).allowed


def test_global_deny_blocks_matching_request():
    decision = evaluate(_policies([_rule(id="no-prod")]), PolicyAction.NODE_DRAIN, _PROD)
    assert not decision.allowed
    assert decision.rule_id == "no-prod"
    assert decision.layer == "global"
    assert "prod-1" in decision.reason


def test_deny_does_not_block_non_matching_request():
    assert evaluate(_policies([_rule()]), PolicyAction.NODE_DRAIN, _DEV).allowed


def test_deny_does_not_apply_to_other_actions():
    assert evaluate(_policies([_rule()]), PolicyAction.NODE_CORDON, _PROD).allowed


def test_regex_is_a_full_match_not_a_search():
    """`prod` must not catch `nonprod-1`."""
    rule = _rule(match={"cluster": "prod"})
    assert evaluate(_policies([rule]), PolicyAction.NODE_DRAIN, {PolicyParam.CLUSTER: "nonprod", PolicyParam.NODE: "n"}).allowed
    assert not evaluate(_policies([rule]), PolicyAction.NODE_DRAIN, {PolicyParam.CLUSTER: "prod", PolicyParam.NODE: "n"}).allowed


def test_keys_within_one_rule_are_anded():
    rule = _rule(match={"cluster": "prod-.*", "node": ".*-master-\\d+"})
    p = _policies([rule])
    assert evaluate(p, PolicyAction.NODE_DRAIN, {PolicyParam.CLUSTER: "prod-1", PolicyParam.NODE: "worker-1"}).allowed
    assert evaluate(p, PolicyAction.NODE_DRAIN, {PolicyParam.CLUSTER: "dev-1", PolicyParam.NODE: "cp-master-0"}).allowed
    assert not evaluate(p, PolicyAction.NODE_DRAIN, {PolicyParam.CLUSTER: "prod-1", PolicyParam.NODE: "cp-master-0"}).allowed


def test_deny_with_absent_param_does_not_match():
    rule = _rule(match={"node": "master-.*"})
    assert evaluate(_policies([rule]), PolicyAction.NODE_DRAIN, {PolicyParam.CLUSTER: "prod-1"}).allowed


def test_description_is_part_of_the_reason():
    rule = _rule(description="prod is managed by the SRE pipeline")
    decision = evaluate(_policies([rule]), PolicyAction.NODE_DRAIN, _PROD)
    assert "prod is managed by the SRE pipeline" in decision.reason


# ── allow ─────────────────────────────────────────────────────────────────────


def test_allow_list_admits_only_matching_requests():
    allow = _rule(id="dev-only", effect="allow", match={"cluster": "dev-.*"})
    p = _policies(user_rules=[allow])
    assert evaluate(p, PolicyAction.NODE_DRAIN, _DEV).allowed

    decision = evaluate(p, PolicyAction.NODE_DRAIN, _PROD)
    assert not decision.allowed
    assert decision.layer == "user"
    assert decision.rule_id is None
    assert "no allow rule" in decision.reason.lower()


def test_allow_list_only_restricts_the_actions_it_names():
    allow = _rule(effect="allow", actions=["node.drain"], match={"cluster": "dev-.*"})
    assert evaluate(_policies(user_rules=[allow]), PolicyAction.NODE_CORDON, _PROD).allowed


def test_any_allow_rule_in_a_layer_is_enough():
    p = _policies(user_rules=[
        _rule(id="a", effect="allow", match={"cluster": "dev-.*"}),
        _rule(id="b", effect="allow", match={"cluster": "stg-.*"}),
    ])
    assert evaluate(p, PolicyAction.NODE_DRAIN, {PolicyParam.CLUSTER: "stg-2", PolicyParam.NODE: "n"}).allowed


def test_allow_with_absent_param_does_not_admit():
    allow = _rule(effect="allow", match={"node": "worker-.*"})
    assert not evaluate(_policies([allow]), PolicyAction.NODE_DRAIN, {PolicyParam.CLUSTER: "dev-1"}).allowed


def test_deny_wins_over_allow_in_the_same_layer():
    p = _policies(user_rules=[
        _rule(id="all", effect="allow", match={"cluster": ".*"}),
        _rule(id="no-master", effect="deny", match={"node": ".*-master-\\d+"}),
    ])
    decision = evaluate(p, PolicyAction.NODE_DRAIN, {PolicyParam.CLUSTER: "dev-1", PolicyParam.NODE: "cp-master-0"})
    assert not decision.allowed
    assert decision.rule_id == "no-master"


# ── layering ──────────────────────────────────────────────────────────────────


def test_user_allow_cannot_override_global_deny():
    """A user rule only tightens: there is no per-user exemption."""
    p = _policies(
        global_rules=[_rule(id="no-prod")],
        user_rules=[_rule(id="me-prod", effect="allow", match={"cluster": "prod-.*"})],
    )
    decision = evaluate(p, PolicyAction.NODE_DRAIN, _PROD)
    assert not decision.allowed
    assert decision.rule_id == "no-prod"


def test_both_layers_allow_lists_must_be_satisfied():
    p = _policies(
        global_rules=[_rule(id="g", effect="allow", match={"cluster": "(dev|stg)-.*"})],
        user_rules=[_rule(id="u", effect="allow", match={"cluster": "dev-.*"})],
    )
    assert evaluate(p, PolicyAction.NODE_DRAIN, _DEV).allowed
    assert not evaluate(p, PolicyAction.NODE_DRAIN, {PolicyParam.CLUSTER: "stg-1", PolicyParam.NODE: "n"}).allowed
    assert not evaluate(p, PolicyAction.NODE_DRAIN, _PROD).allowed


def test_an_unknown_action_string_is_refused_not_allowed():
    """No rule could match an unknown action, so letting it through would fail
    open; evaluate converts the action and raises instead."""
    with pytest.raises(ValueError, match="node.explode"):
        evaluate(_policies(), "node.explode", _PROD)


def test_every_action_declares_its_parameters():
    assert set(POLICY_ACTIONS) == set(PolicyAction)
    assert set().union(*POLICY_ACTIONS.values()) == set(PolicyParam)


def test_error_messages_name_actions_as_plain_strings():
    """An enum repr must not leak into what a user reads."""
    with pytest.raises(ValidationError) as info:
        _rule(actions=["node.drian"])
    assert "PolicyAction" not in str(info.value)
    assert "'node.cordon'" in str(info.value)
