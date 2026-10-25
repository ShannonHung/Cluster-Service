"""
app/domain/access_policy.py

Request-parameter access policy: which write actions a caller may perform on
which clusters / nodes / namespaces. Rules live in USERS_JSON_PATH next to the
accounts — a top-level ``policies`` list applies to every user, and each user
entry may carry its own ``policies``.

Evaluation, per layer (global first, then the caller's own):
  1. a matching ``deny`` rule refuses;
  2. if the layer has any ``allow`` rule for the action, one must match;
  3. otherwise the layer does not restrict the action.
The request passes only when **both** layers pass, so a user rule can tighten
the global policy but never loosen it — there is no per-user exemption, admin
included.

Within one rule the ``match`` keys are ANDed and each value is a regex that
must ``fullmatch`` the request value (``prod`` does not catch ``nonprod``). A
rule naming a parameter the request does not carry does not match: a deny is
then inert, an allow does not admit.

Everything a typo could make silently inert — an unknown action, a match key
the targeted actions do not have, a regex that does not compile — is refused
when the file is parsed, so a broken policy fails closed instead of open.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

PolicyAction = Literal[
    "node.cordon",
    "node.uncordon",
    "node.drain",
    "node.label",
    "node.annotate",
    "node.taint",
    "namespace.create",
    "namespace.label",
]

# Every policed action and the parameters a rule may match on. Read actions are
# deliberately absent: the policy gates writes only. Keys mirror PolicyAction.
POLICY_ACTIONS: dict[PolicyAction, frozenset[str]] = {
    "node.cordon":      frozenset({"cluster", "node"}),
    "node.uncordon":    frozenset({"cluster", "node"}),
    "node.drain":       frozenset({"cluster", "node"}),
    "node.label":       frozenset({"cluster", "node"}),
    "node.annotate":    frozenset({"cluster", "node"}),
    "node.taint":       frozenset({"cluster", "node"}),
    "namespace.create": frozenset({"cluster", "namespace"}),
    "namespace.label":  frozenset({"cluster", "namespace"}),
}

Layer = Literal["global", "user"]


class PolicyRule(BaseModel):
    """One allow / deny rule as written in USERS_JSON_PATH."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    comment: Optional[str] = Field(default=None, alias="$comment", description="Free text, ignored.")
    id: str = Field(min_length=1)
    description: Optional[str] = None
    actions: list[str] = Field(
        min_length=1,
        description="Action names; a trailing glob (`node.*`, `*`) is allowed.",
    )
    effect: Literal["allow", "deny"]
    match: dict[str, str] = Field(
        min_length=1,
        description="Parameter name → regex, every entry must fullmatch.",
    )

    _targets: frozenset[str] = PrivateAttr(default_factory=frozenset)
    _patterns: dict[str, re.Pattern[str]] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _resolve(self) -> "PolicyRule":
        targets: set[str] = set()
        for pattern in self.actions:
            hits = {a for a in POLICY_ACTIONS if fnmatchcase(a, pattern)}
            if not hits:
                raise ValueError(
                    f"rule '{self.id}': action '{pattern}' matches no policed "
                    f"action (known: {sorted(POLICY_ACTIONS)})"
                )
            targets |= hits

        params = set().union(*(POLICY_ACTIONS[a] for a in targets))
        for key in self.match:
            if key not in params:
                raise ValueError(
                    f"rule '{self.id}': match key '{key}' is not a parameter of "
                    f"{sorted(targets)} (known: {sorted(params)})"
                )

        patterns: dict[str, re.Pattern[str]] = {}
        for key, regex in self.match.items():
            try:
                patterns[key] = re.compile(regex)
            except re.error as exc:
                raise ValueError(
                    f"rule '{self.id}': invalid regex for '{key}': {regex!r} ({exc})"
                ) from exc

        self._targets = frozenset(targets)
        self._patterns = patterns
        return self

    def applies_to(self, action: str) -> bool:
        return action in self._targets

    def matches(self, params: Mapping[str, Optional[str]]) -> bool:
        for key, pattern in self._patterns.items():
            value = params.get(key)
            if value is None or pattern.fullmatch(value) is None:
                return False
        return True


@dataclass(frozen=True)
class AccessPolicies:
    """The two rule layers that apply to one caller."""

    account: str
    global_rules: list[PolicyRule]
    user_rules: list[PolicyRule]


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""
    layer: Optional[Layer] = None
    # The deny rule that fired; None for an allow-list miss (no single rule).
    rule_id: Optional[str] = None


_ALLOWED = PolicyDecision(allowed=True)


def _describe(params: Mapping[str, Optional[str]]) -> str:
    return ", ".join(f"{k}='{v}'" for k, v in params.items() if v is not None)


def _evaluate_layer(
    rules: list[PolicyRule],
    layer: Layer,
    account: str,
    action: PolicyAction,
    params: Mapping[str, Optional[str]],
) -> PolicyDecision:
    applicable = [r for r in rules if r.applies_to(action)]
    where = "global policy" if layer == "global" else f"policy of user '{account}'"

    for rule in applicable:
        if rule.effect == "deny" and rule.matches(params):
            reason = f"{action} denied by {where} rule '{rule.id}' ({_describe(params)})"
            if rule.description:
                reason += f": {rule.description}"
            return PolicyDecision(False, reason, layer, rule.id)

    allows = [r for r in applicable if r.effect == "allow"]
    if allows and not any(r.matches(params) for r in allows):
        reason = (
            f"{action} denied: no allow rule in {where} permits "
            f"{_describe(params)} (allowed by: {[r.id for r in allows]})"
        )
        return PolicyDecision(False, reason, layer, None)

    return _ALLOWED


def evaluate(
    policies: AccessPolicies,
    action: PolicyAction,
    params: Mapping[str, Optional[str]],
) -> PolicyDecision:
    """Judge one request. Raises ``KeyError`` for an action not in POLICY_ACTIONS
    — that is a routing bug, never a caller error."""
    if action not in POLICY_ACTIONS:
        raise KeyError(f"'{action}' is not a policed action")

    for rules, layer in (
        (policies.global_rules, "global"),
        (policies.user_rules, "user"),
    ):
        decision = _evaluate_layer(rules, layer, policies.account, action, params)
        if not decision.allowed:
            return decision
    return _ALLOWED
