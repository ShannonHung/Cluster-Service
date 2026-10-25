"""
tests/unit/test_json_users_file.py

USERS_JSON_PATH as read by both repositories: both file shapes, per-user
policy layering, hot reload, and failing closed on a broken file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.core.exceptions import UsersFileInvalidException
from app.repositories.json_policy_repository import JsonPolicyRepository
from app.repositories.json_user_repository import JsonUserRepository
from app.repositories.json_users_file import JsonUsersFile

_DENY_PROD = {
    "id": "no-prod",
    "actions": ["node.drain"],
    "effect": "deny",
    "match": {"cluster": "prod-.*"},
}
_HASH = "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW"
_ALICE = {"account": "alice", "hashed_password": _HASH, "scopes": ["cluster_api"]}


def _write(path: Path, content) -> Path:
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


def _policy_repo(path: Path) -> JsonPolicyRepository:
    return JsonPolicyRepository(JsonUsersFile(path))


def _bump_mtime(path: Path) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


async def test_legacy_list_file_has_no_policies(tmp_path):
    repo = _policy_repo(_write(tmp_path / "u.json", [_ALICE]))
    policies = await repo.get_policies("alice")
    assert policies.global_rules == []
    assert policies.user_rules == []


async def test_global_and_user_rules_are_separated(tmp_path):
    user_rule = {**_DENY_PROD, "id": "alice-no-master", "match": {"node": "master-.*"}}
    path = _write(tmp_path / "u.json", {
        "policies": [_DENY_PROD],
        "users": [{**_ALICE, "policies": [user_rule]}, {**_ALICE, "account": "bob"}],
    })
    repo = _policy_repo(path)

    alice = await repo.get_policies("alice")
    assert [r.id for r in alice.global_rules] == ["no-prod"]
    assert [r.id for r in alice.user_rules] == ["alice-no-master"]

    bob = await repo.get_policies("bob")
    assert [r.id for r in bob.global_rules] == ["no-prod"]
    assert bob.user_rules == []


async def test_unknown_account_still_gets_global_rules(tmp_path):
    repo = _policy_repo(_write(tmp_path / "u.json", {"policies": [_DENY_PROD], "users": []}))
    assert [r.id for r in (await repo.get_policies("ghost")).global_rules] == ["no-prod"]


async def test_edits_are_picked_up_without_restart(tmp_path):
    path = _write(tmp_path / "u.json", {"policies": [], "users": [_ALICE]})
    repo = _policy_repo(path)
    assert (await repo.get_policies("alice")).global_rules == []

    _write(path, {"policies": [_DENY_PROD], "users": [_ALICE]})
    _bump_mtime(path)
    assert [r.id for r in (await repo.get_policies("alice")).global_rules] == ["no-prod"]


@pytest.mark.parametrize(
    "content",
    [
        {"policies": [{**_DENY_PROD, "match": {"cluster": "prod-("}}], "users": []},
        {"policies": [{**_DENY_PROD, "actions": ["node.drian"]}], "users": []},
        {"policies": [_DENY_PROD, _DENY_PROD], "users": []},  # duplicate id
        {"policies": {"id": "not-a-list"}, "users": []},
        {"users": [{**_ALICE, "policies": [{"id": "x"}]}]},
        {"users": [{**_ALICE, "hashed_password": "secret"}]},
        "just a string",
    ],
    ids=["bad-regex", "unknown-action", "duplicate-id", "policies-not-list", "bad-user-rule",
         "plaintext-password", "bad-shape"],
)
async def test_broken_policy_file_fails_closed(tmp_path, content):
    repo = _policy_repo(_write(tmp_path / "u.json", content))
    with pytest.raises(UsersFileInvalidException):
        await repo.get_policies("alice")


async def test_unparseable_json_fails_closed(tmp_path):
    path = tmp_path / "u.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(UsersFileInvalidException):
        await _policy_repo(path).get_policies("alice")


async def test_missing_file_fails_closed(tmp_path):
    with pytest.raises(UsersFileInvalidException):
        await _policy_repo(tmp_path / "absent.json").get_policies("alice")


async def test_a_broken_edit_is_not_masked_by_the_cache(tmp_path):
    """Fail closed means the *current* file decides, not the last good one."""
    path = _write(tmp_path / "u.json", {"policies": [], "users": []})
    repo = _policy_repo(path)
    await repo.get_policies("alice")

    path.write_text("{not json", encoding="utf-8")
    _bump_mtime(path)
    with pytest.raises(UsersFileInvalidException):
        await repo.get_policies("alice")


# ── the user repository reads the same file ───────────────────────────────────


async def test_user_repository_reads_the_object_format(tmp_path):
    path = _write(tmp_path / "u.json", {
        "policies": [_DENY_PROD],
        "users": [{**_ALICE, "policies": [{**_DENY_PROD, "id": "alice-no-prod"}]}],
    })
    repo = JsonUserRepository(JsonUsersFile(path))
    user = await repo.get_by_account("alice")
    assert user is not None and user.scopes == ["cluster_api"]
    assert await repo.list_accounts() == ["alice"]


async def test_user_repository_still_reads_the_legacy_list(tmp_path):
    repo = JsonUserRepository(JsonUsersFile(_write(tmp_path / "u.json", [_ALICE])))
    assert await repo.list_accounts() == ["alice"]


async def test_login_is_refused_while_the_file_is_invalid(tmp_path):
    """A bad entry anywhere fails the whole file — not only the user it is on."""
    path = _write(tmp_path / "u.json", [_ALICE, {"account": "bob"}])
    with pytest.raises(UsersFileInvalidException, match=r"users\.1\.hashed_password"):
        await JsonUserRepository(JsonUsersFile(path)).get_by_account("alice")


async def test_the_error_does_not_leak_the_path(tmp_path):
    path = _write(tmp_path / "u.json", "nope")
    with pytest.raises(UsersFileInvalidException) as info:
        await JsonUserRepository(JsonUsersFile(path)).get_by_account("alice")
    assert str(tmp_path) not in info.value.message


async def test_a_same_size_replace_is_picked_up(tmp_path):
    """Same length, same mtime (pinned): only the inode tells the files apart —
    the usual atomic write-temp-then-rename edit."""
    path = _write(tmp_path / "u.json", {"policies": [{**_DENY_PROD, "id": "rule-a"}], "users": []})
    st = path.stat()
    repo = _policy_repo(path)
    assert [r.id for r in (await repo.get_policies("x")).global_rules] == ["rule-a"]

    tmp = _write(tmp_path / "u.json.tmp", {"policies": [{**_DENY_PROD, "id": "rule-b"}], "users": []})
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    os.replace(tmp, path)
    assert path.stat().st_size == st.st_size and path.stat().st_mtime_ns == st.st_mtime_ns
    assert [r.id for r in (await repo.get_policies("x")).global_rules] == ["rule-b"]


async def test_an_unreadable_path_does_not_leak_into_the_error(tmp_path):
    """OSError's str() embeds the filename; the response must not."""
    target = tmp_path / "users-dir"
    target.mkdir()
    with pytest.raises(UsersFileInvalidException) as info:
        JsonUsersFile(target).load()
    assert str(tmp_path) not in info.value.message
    assert "Is a directory" in info.value.message
