"""
tests/unit/test_users_document.py

The whole USERS_JSON_PATH file is validated in one pass — accounts and access
policy alike — so a mistake is found when the file is loaded (start-up, or
`make check-users`), not at the first login that happens to hit it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.access_policy import POLICY_ACTIONS
from app.domain.users_document import UsersDocumentError, parse_users_document

# Deliberately not a bcrypt hash — the input the validator must refuse.
# Named rather than inlined next to the password key, so secret scanners
# do not read a test input as a leaked credential.
_NOT_A_HASH = "plain-text-value"

_HASH = "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW"
_ALICE = {"account": "alice", "hashed_password": _HASH, "scopes": ["cluster_api"]}
_RULE = {"id": "no-prod", "actions": ["node.drain"], "effect": "deny", "match": {"cluster": "prod-.*"}}
_ROOT = Path(__file__).parent.parent.parent


def test_legacy_list_is_users_without_policy():
    doc = parse_users_document([_ALICE])
    assert [u.account for u in doc.users] == ["alice"]
    assert doc.policies == []


def test_object_form_carries_both_layers():
    doc = parse_users_document({"policies": [_RULE], "users": [{**_ALICE, "policies": [{**_RULE, "id": "x"}]}]})
    assert [r.id for r in doc.policies] == ["no-prod"]
    assert [r.id for r in doc.users[0].policies] == ["x"]


@pytest.mark.parametrize(
    "users, fragment",
    [
        ([{"hashed_password": _HASH}], "account"),
        ([{"account": "alice"}], "hashed_password"),
        ([{**_ALICE, "hashed_password": _NOT_A_HASH}], "bcrypt"),
        ([{**_ALICE, "scopes": "cluster_api"}], "scopes"),
        ([{**_ALICE, "polices": []}], "polices"),  # typo would drop the rules
        ([_ALICE, _ALICE], "duplicate account"),
        ([{**_ALICE, "account": ""}], "account"),
    ],
    ids=["no-account", "no-hash", "plaintext-password", "scopes-not-list",
         "unknown-user-field", "duplicate-account", "blank-account"],
)
def test_bad_user_entries_are_refused(users, fragment):
    with pytest.raises(UsersDocumentError, match=fragment):
        parse_users_document({"users": users})


def test_unknown_top_level_key_is_refused():
    with pytest.raises(UsersDocumentError, match="policy"):
        parse_users_document({"policy": [_RULE], "users": [_ALICE]})


def test_rule_ids_are_unique_across_the_file():
    with pytest.raises(UsersDocumentError, match="duplicate policy rule id"):
        parse_users_document({"policies": [_RULE], "users": [{**_ALICE, "policies": [_RULE]}]})


def test_a_bad_rule_names_where_it_is():
    with pytest.raises(UsersDocumentError) as info:
        parse_users_document({"users": [_ALICE, {**_ALICE, "account": "bob", "policies": [{**_RULE, "actions": ["node.drian"]}]}]})
    assert any(p.startswith("users.1.policies.0") for p in info.value.problems)


def test_every_problem_is_reported_in_one_pass():
    """A duplicate is not held back until the field errors are fixed."""
    with pytest.raises(UsersDocumentError) as info:
        parse_users_document({
            "policies": [_RULE, {**_RULE, "match": {"cluster": "prod-("}}],
            "users": [{**_ALICE, "hashed_password": _NOT_A_HASH}, _ALICE],
        })
    problems = "\n".join(info.value.problems)
    assert "users.0.hashed_password: is not a bcrypt hash" in problems
    assert "policies.1: rule 'no-prod': invalid regex" in problems
    assert "duplicate account(s): ['alice']" in problems
    assert "duplicate policy rule id(s): ['no-prod']" in problems


def test_comments_are_accepted_at_every_level():
    doc = parse_users_document({
        "$comment": "file",
        "policies": [{**_RULE, "$comment": "rule"}],
        "users": [{**_ALICE, "$comment": "user"}],
    })
    assert doc.policies[0].applies_to("node.drain")


def test_neither_list_nor_object_is_refused():
    with pytest.raises(UsersDocumentError):
        parse_users_document("nope")


# ── the shipped files ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["data/users.json", "data/users.example.json", "tests/fixtures/users.json"])
def test_shipped_users_files_are_valid(path):
    parse_users_document(json.loads((_ROOT / path).read_text()))


def test_the_example_shows_every_policed_action():
    """The example is the user's reference — it must not fall behind the code."""
    text = (_ROOT / "data/users.example.json").read_text()
    missing = [a for a in POLICY_ACTIONS if f'"{a}"' not in text]
    assert missing == []


def test_the_readme_documents_every_policed_action():
    text = (_ROOT / "README.md").read_text()
    missing = [a for a in POLICY_ACTIONS if f"`{a}`" not in text]
    assert missing == []


def _example_policies(account: str):
    from app.domain.access_policy import AccessPolicies

    doc = parse_users_document(json.loads((_ROOT / "data/users.example.json").read_text()))
    user = next(u for u in doc.users if u.account == account)
    return AccessPolicies(account, doc.policies, user.policies)


@pytest.mark.parametrize(
    "account, action, params, allowed",
    [
        # global rules bind everyone, admin included
        ("admin", "node.drain", {"cluster": "prod-east", "node": "worker-1"}, False),
        ("admin", "node.drain", {"cluster": "dev-1", "node": "cp-master-0"}, False),
        ("admin", "node.drain", {"cluster": "dev-1", "node": "worker-1"}, True),
        ("admin", "namespace.label", {"cluster": "dev-1", "namespace": "istio-system"}, False),
        # operator: allow list for the actions it names
        ("operator", "node.drain", {"cluster": "stg-2", "node": "worker-1"}, True),
        ("operator", "node.drain", {"cluster": "qa-1", "node": "worker-1"}, False),
        ("operator", "node.drain", {"cluster": "prod-east", "node": "worker-1"}, False),
        ("operator", "node.taint", {"cluster": "dev-1", "node": "worker-1"}, False),
        # an action in no operator rule: only the global rules apply
        ("operator", "namespace.label", {"cluster": "qa-1", "namespace": "team-a"}, True),
        # viewer: every write refused
        ("viewer", "node.cordon", {"cluster": "dev-1", "node": "worker-1"}, False),
        ("viewer", "namespace.create", {"cluster": "dev-1", "namespace": "team-a"}, False),
    ],
)
def test_the_example_behaves_as_its_comments_say(account, action, params, allowed):
    from app.domain.access_policy import evaluate

    assert evaluate(_example_policies(account), action, params).allowed is allowed
