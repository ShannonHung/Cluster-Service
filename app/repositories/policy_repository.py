"""
app/repositories/policy_repository.py

Abstract interface for the request-parameter access policy store.
PolicyService depends on this; JsonPolicyRepository implements it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.access_policy import AccessPolicies


class PolicyRepository(ABC):
    """Abstract contract for access-policy lookup."""

    @abstractmethod
    async def get_policies(self, account: str) -> AccessPolicies:
        """Return the global rules plus *account*'s own rules.

        An account with no entry gets the global rules only. Raises
        ``UsersFileInvalidException`` when the store cannot be read or parsed —
        never an empty policy in its place.
        """

