"""
app/domain/users_document.py

The schema of the USERS_JSON_PATH file — accounts and access policy — checked
in one pass, so every mistake is found when the file is loaded rather than by
the first login or write that happens to touch it. Two shapes are accepted:

    [ {user}, ... ]                                  # legacy: accounts only
    { "policies": [ {rule}, ... ],                   # rules for every user
      "users":    [ {user, "policies": [...]}, ... ] }

Unknown keys are refused at every level: a typo such as ``"polices"`` would
otherwise drop that user's rules without a word. JSON has no comments, so the
document, each user and each rule accept a free-text ``"$comment"`` instead.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.domain.access_policy import PolicyRule
from app.domain.models import UserInDB

# bcrypt modular-crypt form: $2a$ / $2b$ / $2y$, two-digit cost, 53 chars of
# salt + hash. Catches a plaintext password pasted in place of `make hash`.
_BCRYPT_PATTERN = r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}"


class UserEntry(UserInDB):
    """One account as written in the file, its own policy rules included."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    comment: Optional[str] = Field(default=None, alias="$comment")
    account: str = Field(min_length=1)
    hashed_password: str = Field(
        description="bcrypt hash (`make hash p=...`), never a plaintext password",
    )
    policies: list[PolicyRule] = Field(default_factory=list)

    @field_validator("hashed_password")
    @classmethod
    def _is_bcrypt(cls, value: str) -> str:
        if not re.fullmatch(_BCRYPT_PATTERN, value):
            raise ValueError("is not a bcrypt hash — generate one with `make hash p=<password>`")
        return value


class UsersDocument(BaseModel):
    """The validated file. Build it with ``parse_users_document``, which also
    checks the cross-entry rules (unique accounts, unique rule ids)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    comment: Optional[str] = Field(default=None, alias="$comment")
    policies: list[PolicyRule] = Field(default_factory=list)
    users: list[UserEntry] = Field(default_factory=list)


class UsersDocumentError(ValueError):
    """Every problem found in a users file, each as ``<location>: <message>``."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def parse_users_document(raw: Any) -> UsersDocument:
    """Validate a decoded users file, reporting **every** problem at once.

    The cross-entry checks run on the raw input next to the per-field ones,
    rather than after them, so one pass of ``make check-users`` lists a
    duplicate account alongside a malformed hash instead of only once the hash
    is fixed.
    """
    if isinstance(raw, list):
        raw = {"users": raw}
    if not isinstance(raw, dict):
        raise UsersDocumentError(["<file>: must be a list of users or an object with 'users'"])

    problems = _duplicates(raw)
    try:
        doc = UsersDocument.model_validate(raw)
    except ValidationError as exc:
        problems = [_describe(e) for e in exc.errors()] + problems
    if problems:
        raise UsersDocumentError(problems)
    return doc


def _describe(error: Any) -> str:
    where = ".".join(str(p) for p in error["loc"]) or "<file>"
    return f"{where}: {error['msg'].removeprefix('Value error, ')}"


def _duplicates(raw: dict) -> list[str]:
    def entries(value: Any) -> list[dict]:
        return [e for e in value if isinstance(e, dict)] if isinstance(value, list) else []

    def dupes(values: list[Any]) -> list[str]:
        named = [v for v in values if isinstance(v, str)]
        return sorted({v for v in named if named.count(v) > 1})

    users = entries(raw.get("users"))
    problems: list[str] = []
    if accounts := dupes([u.get("account") for u in users]):
        problems.append(f"users: duplicate account(s): {accounts}")

    # Ids name the rule in every denial and audit line; a duplicate would
    # make "denied by rule X" ambiguous.
    rules = entries(raw.get("policies")) + [r for u in users for r in entries(u.get("policies"))]
    if ids := dupes([r.get("id") for r in rules]):
        problems.append(f"policies: duplicate policy rule id(s): {ids}")
    return problems
