"""
app/domain/label_rules.py

Kubernetes naming rules shared by the label validation and the provenance
annotation prefix (CONTEXT.md "Namespaces": Reserved label key, Provenance
annotations).
"""

from __future__ import annotations

import re

# A DNS-1123 subdomain: dot-separated DNS-1123 labels, at most 253 characters.
_DNS_SUBDOMAIN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")
DNS_SUBDOMAIN_MAX = 253

# Domains Kubernetes reserves for itself. Fixed, not configurable.
_RESERVED_DOMAINS = ("kubernetes.io", "k8s.io")


def is_dns_subdomain(value: str) -> bool:
    return len(value) <= DNS_SUBDOMAIN_MAX and bool(_DNS_SUBDOMAIN.match(value))


def _is_domain_or_subdomain(domain: str, root: str) -> bool:
    """`root` itself or any subdomain of it — not a domain that merely ends in
    the same letters (`notkubernetes.io`)."""
    return domain == root or domain.endswith("." + root)


def is_reserved_domain(domain: str) -> bool:
    """True for `kubernetes.io`, `k8s.io` and any subdomain of either."""
    return any(_is_domain_or_subdomain(domain, d) for d in _RESERVED_DOMAINS)


def is_under_domain(key: str, domain: str) -> bool:
    """True when ``key``'s prefix (before the `/`) is ``domain`` or a subdomain
    of it. A key without a prefix is under no domain."""
    prefix, slash, _ = key.rpartition("/")
    return bool(slash) and _is_domain_or_subdomain(prefix, domain)


# A label name (the part after the optional `prefix/`) and a non-empty label
# value share one shape: at most 63 characters, alphanumeric at both ends,
# `-`, `_` and `.` in between.
_LABEL_NAME = re.compile(r"^[A-Za-z0-9]([-A-Za-z0-9_.]*[A-Za-z0-9])?$")
LABEL_NAME_MAX = 63


def _malformed_key(key: str) -> bool:
    prefix, slash, name = key.rpartition("/")
    if slash and not is_dns_subdomain(prefix):
        return True
    return len(name) > LABEL_NAME_MAX or not _LABEL_NAME.match(name)


def _malformed_value(value: str) -> bool:
    return value != "" and (len(value) > LABEL_NAME_MAX or not _LABEL_NAME.match(value))


def is_reserved_label_key(key: str) -> bool:
    """CONTEXT.md, Reserved label key: a prefix of kubernetes.io, k8s.io or a
    subdomain of either. Kubernetes acts on these labels."""
    prefix, slash, _ = key.rpartition("/")
    return bool(slash) and is_reserved_domain(prefix)


def check_label_keys(keys: list[str]) -> None:
    """Raise ValueError naming every malformed and every reserved key at once,
    so one refused request reveals the whole set rather than one per retry."""
    malformed = [k for k in keys if _malformed_key(k)]
    reserved = [k for k in keys if k not in malformed and is_reserved_label_key(k)]
    problems = []
    if malformed:
        problems.append(f"malformed label keys {malformed}")
    if reserved:
        problems.append(
            f"label keys {reserved} are reserved for Kubernetes (kubernetes.io, k8s.io)"
        )
    if problems:
        raise ValueError("; ".join(problems))


def check_annotation_keys(keys: list[str]) -> None:
    """Raise ValueError naming every malformed annotation key at once. An
    annotation key has the shape of a label key; unlike labels, no domain is
    reserved here and the values are free-form, so they are not checked."""
    malformed = [k for k in keys if _malformed_key(k)]
    if malformed:
        raise ValueError(f"malformed annotation keys {malformed}")


def check_labels(labels: dict[str, str]) -> None:
    """`check_label_keys`, plus malformed values — all named at once."""
    problems = []
    try:
        check_label_keys(list(labels))
    except ValueError as exc:
        problems.append(str(exc))
    bad_values = [k for k, v in labels.items() if _malformed_value(v)]
    if bad_values:
        problems.append(f"malformed label values for keys {bad_values}")
    if problems:
        raise ValueError("; ".join(problems))
