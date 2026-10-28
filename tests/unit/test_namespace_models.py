"""
tests/unit/test_namespace_models.py

Unit tests for the namespace request models. Vocabulary follows CONTEXT.md,
"Namespaces". The provenance prefix is a setting the models cannot see, so its
check lives in NamespaceService (test_namespace_service.py).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.domain.kubernetes_models import NamespaceCreateRequest


def test_a_create_request_may_carry_annotations():
    body = NamespaceCreateRequest(name="team-a", annotations={"owner": "alice"})

    assert body.annotations == {"owner": "alice"}


def test_a_create_request_without_annotations_has_none():
    assert NamespaceCreateRequest(name="team-a").annotations == {}


def test_malformed_annotation_keys_are_refused_naming_every_one():
    with pytest.raises(ValidationError) as info:
        NamespaceCreateRequest(
            name="team-a",
            annotations={"-bad": "x", "Bad Key": "y", "also/ /bad": "z", "ok": "fine"},
        )

    text = str(info.value)
    for key in ("-bad", "Bad Key", "also/ /bad"):
        assert key in text


def test_an_annotation_value_may_be_any_string():
    """Unlike a label value, an annotation value is free-form: long, spaced,
    JSON, or empty."""
    values = {"a": "", "b": "x" * 500, "c": '{"k": [1, 2]}', "d": "has spaces & symbols!"}

    assert NamespaceCreateRequest(name="team-a", annotations=values).annotations == values


def test_an_annotation_key_may_carry_a_domain_prefix():
    body = NamespaceCreateRequest(name="team-a", annotations={"example.com/owner": "alice"})

    assert body.annotations == {"example.com/owner": "alice"}
