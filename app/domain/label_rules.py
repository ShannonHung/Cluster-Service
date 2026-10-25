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


def is_reserved_domain(domain: str) -> bool:
    """True for `kubernetes.io`, `k8s.io` and any subdomain of either — but not
    for a domain that merely ends in the same letters (`notkubernetes.io`)."""
    return any(domain == d or domain.endswith("." + d) for d in _RESERVED_DOMAINS)
