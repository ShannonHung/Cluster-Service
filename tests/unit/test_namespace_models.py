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


def test_annotation_keys_reserved_for_kubernetes_are_refused_naming_every_one():
    """Kubernetes admission plugins act on some of these (e.g. the scheduler's
    node-selector), so a caller may not set them any more than a Reserved label
    key."""
    with pytest.raises(ValidationError) as info:
        NamespaceCreateRequest(
            name="team-a",
            annotations={
                "scheduler.alpha.kubernetes.io/node-selector": "x=y",
                "k8s.io/anything": "1",
                "kubectl.kubernetes.io/last-applied-configuration": "{}",
                "owner": "alice",
            },
        )

    message = info.value.errors()[0]["msg"]
    for key in (
        "scheduler.alpha.kubernetes.io/node-selector",
        "k8s.io/anything",
        "kubectl.kubernetes.io/last-applied-configuration",
    ):
        assert key in message
    assert "owner" not in message


def test_a_domain_that_only_resembles_a_reserved_one_is_allowed_as_an_annotation_key():
    body = NamespaceCreateRequest(
        name="team-a", annotations={"notkubernetes.io/x": "1", "kubernetes.io.evil/x": "2"}
    )

    assert len(body.annotations) == 2
