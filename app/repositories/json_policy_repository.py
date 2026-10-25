"""
app/repositories/json_policy_repository.py

Reads the access policy from USERS_JSON_PATH — the same file as the accounts,
loaded, validated and cached by ``JsonUsersFile``. An invalid file raises
``UsersFileInvalidException``, so a broken edit fails closed rather than
silently keeping, or dropping, rules.
"""

from __future__ import annotations

from app.domain.access_policy import AccessPolicies
from app.repositories.json_users_file import JsonUsersFile
from app.repositories.policy_repository import PolicyRepository


class JsonPolicyRepository(PolicyRepository):

    def __init__(self, users_file: JsonUsersFile) -> None:
        self._file = users_file

    async def get_policies(self, account: str) -> AccessPolicies:
        doc = self._file.load()
        user = next((u for u in doc.users if u.account == account), None)
        return AccessPolicies(
            account=account,
            global_rules=doc.policies,
            user_rules=user.policies if user is not None else [],
        )
