"""Helpers for reasoning about OpenViking resource URIs."""

from __future__ import annotations


def is_summary_uri(uri: str) -> bool:
    """Whether the URI points at a generated L0/L1 summary file."""
    return uri.rstrip("/").endswith(("/.abstract.md", "/.overview.md"))


def is_generic_scope_summary_uri(uri: str) -> bool:
    """Whether the URI is a root-level scope summary rather than a concrete resource summary."""
    normalized_uri = uri.rstrip("/")
    if not is_summary_uri(normalized_uri):
        return False

    path = normalized_uri.split("://", 1)[-1]
    parts = [part for part in path.split("/") if part]
    if not parts:
        return False

    root = parts[0]
    if root in {"resources", "session"}:
        return len(parts) <= 2
    if root in {"user", "agent"}:
        return len(parts) <= 3
    return False
