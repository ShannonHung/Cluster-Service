"""
app/repositories/json_user_repository.py

JSON file-backed implementation of UserRepository.

The file is loaded through ``JsonUsersFile``, which validates the whole file
(accounts and access policy) and re-reads it whenever it changes — so edits to
users.json are picked up without restarting the service, and an invalid file
refuses login (``UsersFileInvalidException``) instead of half-working.
Suitable for small-scale deployments; replace with a proper DB implementation
(e.g. SqlUserRepository) when needed.
"""

from __future__ import annotations

import logging
from app.domain.models import UserInDB
from app.repositories.json_users_file import JsonUsersFile
from app.repositories.user_repository import UserRepository

_logger = logging.getLogger(__name__)


class JsonUserRepository(UserRepository):
    """Reads user records from a JSON file.

    Expected file format — either a bare list of users, or an object whose
    ``users`` holds that list next to the access policy (see
    ``users_document.py``; policies are read by ``JsonPolicyRepository``)::

        {
            "policies": [],
            "users": [
                {
                    "account": "admin",
                    "hashed_password": "<bcrypt hash>",
                    "scopes": ["cluster_api", "vm_api"]
                }
            ]
        }
    """

    def __init__(self, users_file: JsonUsersFile) -> None:
        self._file = users_file

    # ── interface implementation ───────────────────────────────────────────────

    async def get_by_account(self, account: str) -> UserInDB | None:
        for user in self._file.load().users:
            if user.account == account:
                _logger.debug("User found | account=%s", account)
                return UserInDB(
                    account=user.account,
                    hashed_password=user.hashed_password,
                    scopes=user.scopes,
                )
        _logger.debug("User not found | account=%s", account)
        return None

    async def list_accounts(self) -> list[str]:
        return [u.account for u in self._file.load().users]
