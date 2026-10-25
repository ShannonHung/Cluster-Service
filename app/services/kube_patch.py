"""
app/services/kube_patch.py

The merge-patch shape for a string map in object metadata (labels,
annotations), shared by every service that edits one key by key.
"""

from __future__ import annotations


def key_merge_patch(set_values: dict[str, str], remove_keys: list[str]) -> dict[str, str | None]:
    """``set_values`` as given plus ``None`` for each key in ``remove_keys`` —
    in a merge patch, a null value tells Kubernetes to delete the key, and a key
    the object does not have is a no-op. A key in both is removed."""
    return {**set_values, **dict.fromkeys(remove_keys)}
