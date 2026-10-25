"""
tests/unit/test_provenance_settings.py

PROVENANCE_ANNOTATION_PREFIX — the domain the service's provenance annotations
live under (CONTEXT.md "Namespaces", ADR 0002). It has no default and an
unusable value is refused when settings load, i.e. before the app starts: a
wrong prefix would otherwise be stamped onto every namespace, or make every
create fail at Kubernetes.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**overrides) -> Settings:
    # _env_file=None: only what the test passes counts, not the repo's .env files.
    return Settings(_env_file=None, SECRET_KEY="x" * 32, **overrides)


def test_an_operator_domain_is_accepted():
    assert _settings(PROVENANCE_ANNOTATION_PREFIX="cluster-service.acme.io").PROVENANCE_ANNOTATION_PREFIX == (
        "cluster-service.acme.io"
    )


def test_it_is_required(monkeypatch):
    monkeypatch.delenv("PROVENANCE_ANNOTATION_PREFIX", raising=False)
    with pytest.raises(ValidationError, match="PROVENANCE_ANNOTATION_PREFIX"):
        _settings()


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Acme.io",  # uppercase
        "acme.io/",  # the domain only, no trailing slash
        "-acme.io",
        "acme..io",
        "a" * 254,
    ],
)
def test_a_value_that_is_not_a_dns_subdomain_is_refused(value):
    with pytest.raises(ValidationError, match="PROVENANCE_ANNOTATION_PREFIX"):
        _settings(PROVENANCE_ANNOTATION_PREFIX=value)


@pytest.mark.parametrize(
    "value", ["kubernetes.io", "k8s.io", "app.kubernetes.io", "x.k8s.io"]
)
def test_a_domain_reserved_for_kubernetes_is_refused(value):
    with pytest.raises(ValidationError, match="reserved"):
        _settings(PROVENANCE_ANNOTATION_PREFIX=value)


def test_a_domain_merely_ending_in_the_same_letters_is_not_reserved():
    """`notkubernetes.io` is someone else's domain, not a subdomain of kubernetes.io."""
    assert _settings(PROVENANCE_ANNOTATION_PREFIX="notkubernetes.io")


@pytest.mark.parametrize("env_file", [".env", ".env.dev", ".env.test", ".env.prod"])
def test_every_shipped_env_file_sets_a_usable_value(env_file, monkeypatch):
    monkeypatch.delenv("PROVENANCE_ANNOTATION_PREFIX", raising=False)
    settings = Settings(_env_file=env_file)
    assert settings.PROVENANCE_ANNOTATION_PREFIX
