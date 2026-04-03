# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

from dataclasses import dataclass

import pytest

from openviking.session.memory_extractor import MemoryCategory
from openviking.session.memory_scope import (
    AGENT_MEMORY_SCOPE,
    ALL_MEMORY_SCOPE,
    USER_MEMORY_SCOPE,
    filter_candidates_by_scope,
    normalize_memory_scope,
)


@dataclass
class _Candidate:
    category: MemoryCategory


def test_filter_candidates_by_scope_keeps_only_agent_categories() -> None:
    candidates = [
        _Candidate(MemoryCategory.PROFILE),
        _Candidate(MemoryCategory.EVENTS),
        _Candidate(MemoryCategory.CASES),
        _Candidate(MemoryCategory.SKILLS),
    ]

    filtered = filter_candidates_by_scope(candidates, AGENT_MEMORY_SCOPE)

    assert [candidate.category for candidate in filtered] == [
        MemoryCategory.CASES,
        MemoryCategory.SKILLS,
    ]


def test_filter_candidates_by_scope_keeps_only_user_categories() -> None:
    candidates = [
        _Candidate(MemoryCategory.PROFILE),
        _Candidate(MemoryCategory.PREFERENCES),
        _Candidate(MemoryCategory.CASES),
    ]

    filtered = filter_candidates_by_scope(candidates, USER_MEMORY_SCOPE)

    assert [candidate.category for candidate in filtered] == [
        MemoryCategory.PROFILE,
        MemoryCategory.PREFERENCES,
    ]


def test_filter_candidates_by_scope_keeps_all_when_requested() -> None:
    candidates = [
        _Candidate(MemoryCategory.ENTITIES),
        _Candidate(MemoryCategory.PATTERNS),
    ]

    filtered = filter_candidates_by_scope(candidates, ALL_MEMORY_SCOPE)

    assert filtered == candidates


def test_normalize_memory_scope_rejects_invalid_value() -> None:
    with pytest.raises(ValueError, match="Unsupported memory_scope"):
        normalize_memory_scope("shared")
