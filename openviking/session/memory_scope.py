from __future__ import annotations

from typing import Iterable, Literal, TypeVar, cast

from .memory_extractor import MemoryCategory

MemoryScope = Literal["all", "user", "agent"]

ALL_MEMORY_SCOPE: MemoryScope = "all"
USER_MEMORY_SCOPE: MemoryScope = "user"
AGENT_MEMORY_SCOPE: MemoryScope = "agent"

VALID_MEMORY_SCOPES = {
    ALL_MEMORY_SCOPE,
    USER_MEMORY_SCOPE,
    AGENT_MEMORY_SCOPE,
}

USER_MEMORY_CATEGORIES = frozenset(
    {
        MemoryCategory.PROFILE,
        MemoryCategory.PREFERENCES,
        MemoryCategory.ENTITIES,
        MemoryCategory.EVENTS,
    }
)

AGENT_MEMORY_CATEGORIES = frozenset(
    {
        MemoryCategory.CASES,
        MemoryCategory.PATTERNS,
        MemoryCategory.TOOLS,
        MemoryCategory.SKILLS,
    }
)

T = TypeVar("T")


def normalize_memory_scope(memory_scope: str | None) -> MemoryScope:
    """Normalize and validate a requested memory extraction scope."""
    if memory_scope is None:
        return ALL_MEMORY_SCOPE

    normalized = memory_scope.strip().lower()
    if normalized not in VALID_MEMORY_SCOPES:
        raise ValueError(
            f"Unsupported memory_scope: {memory_scope}. "
            f"Expected one of: {', '.join(sorted(VALID_MEMORY_SCOPES))}"
        )
    return cast(MemoryScope, normalized)


def category_allowed_in_scope(category: MemoryCategory, memory_scope: MemoryScope) -> bool:
    """Return whether one memory category should be processed for the scope."""
    if memory_scope == ALL_MEMORY_SCOPE:
        return True
    if memory_scope == USER_MEMORY_SCOPE:
        return category in USER_MEMORY_CATEGORIES
    return category in AGENT_MEMORY_CATEGORIES


def filter_candidates_by_scope(candidates: Iterable[T], memory_scope: MemoryScope) -> list[T]:
    """Keep only candidates whose category belongs to the requested scope."""
    if memory_scope == ALL_MEMORY_SCOPE:
        return list(candidates)

    filtered: list[T] = []
    for candidate in candidates:
        category = getattr(candidate, "category", None)
        if isinstance(category, MemoryCategory) and category_allowed_in_scope(category, memory_scope):
            filtered.append(candidate)
    return filtered
