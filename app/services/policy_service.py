"""
app/services/policy_service.py

PolicyService — applies the request-parameter access policy
(``app/domain/access_policy.py``) to a write request.

Routers call it **before** any Kubernetes work, with the values the request
will actually act on. Single-resource routes call ``enforce`` (403
``POLICY_DENIED``); batches go through ``NodeBatchService``, which calls
``denied_nodes`` and reports each refused node as a ``POLICY_DENIED`` failure
in ``results`` while the rest of the batch proceeds.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.core.exceptions import PolicyDeniedException
from app.domain.access_policy import (
    AccessPolicies,
    PolicyAction,
    PolicyDecision,
    PolicyParam,
    PolicyParams,
    evaluate,
)
from app.domain.models import User
from app.repositories.policy_repository import PolicyRepository

_logger = logging.getLogger(__name__)


class PolicyService:

    def __init__(self, repo: PolicyRepository) -> None:
        self._repo = repo

    async def enforce(
        self,
        user: User,
        action: PolicyAction,
        params: PolicyParams,
    ) -> None:
        """Raise ``PolicyDeniedException`` unless the policy permits the request."""
        policies = await self._repo.get_policies(user.account)
        decision = self._evaluate_and_log(policies, action, params)
        if not decision.allowed:
            raise PolicyDeniedException(
                decision.reason,
                detail={
                    "action": action,
                    "layer": decision.layer,
                    "rule_id": decision.rule_id,
                    "params": _plain(params),
                },
            )

    async def denied_nodes(
        self,
        user: User,
        action: PolicyAction,
        *,
        cluster: str,
        nodes: list[str],
    ) -> dict[str, str]:
        """Node → denial reason, for each node of a batch the policy refuses.

        The policy is loaded once for the whole batch, so every node is judged
        against the same version of the file.
        """
        policies = await self._repo.get_policies(user.account)
        denied: dict[str, str] = {}
        for node in nodes:
            decision = self._evaluate_and_log(policies, action, {PolicyParam.CLUSTER: cluster, PolicyParam.NODE: node})
            if not decision.allowed:
                denied[node] = decision.reason
        return denied

    @staticmethod
    def _evaluate_and_log(
        policies: AccessPolicies,
        action: PolicyAction,
        params: PolicyParams,
    ) -> PolicyDecision:
        decision = evaluate(policies, action, params)
        if not decision.allowed:
            _logger.warning(
                "Policy denied | user=%s | action=%s | params=%s | layer=%s | rule=%s",
                policies.account, action, _plain(params), decision.layer, decision.rule_id,
            )
        return decision


def _plain(params: PolicyParams) -> dict[str, Optional[str]]:
    """Plain string keys for the response and the log — an enum key's repr
    would leak ``<PolicyParam.CLUSTER: 'cluster'>`` into both."""
    return {str(k): v for k, v in params.items()}
