# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for KB prompt optimizations that reduce search drift and token waste."""

from pathlib import Path

from vikingbot.agent.context import ContextBuilder
from vikingbot.config.schema import CapabilityProfile, Config


def test_kb_initial_search_prompt_prefers_focused_resource_scoped_lookup() -> None:
    config = Config()
    config.agents.capability_profile = CapabilityProfile.KNOWLEDGE_BASE
    builder = ContextBuilder(Path("."), config=config)
    prompt = builder.build_kb_initial_search_prompt()

    assert 'target_uri="viking://resources/"' in prompt
    assert "call retrieval tools directly instead of replying with a prose-only plan." in prompt
    assert "Avoid long OR/boolean query expansions on the first search." in prompt
    assert "Prefer 1 search + 1 read before deciding to broaden." in prompt


def test_kb_continue_search_prompt_reads_concrete_doc_before_new_search() -> None:
    config = Config()
    config.agents.capability_profile = CapabilityProfile.KNOWLEDGE_BASE
    builder = ContextBuilder(Path("."), config=config)
    prompt = builder.build_kb_continue_search_prompt("Current search state")

    assert 'Stay in target_uri="viking://resources/"' in prompt
    assert "If a concrete document URI is already available, read it before any new search." in prompt
    assert "Emit the next retrieval tool call directly." in prompt
    assert "Usually inspect one new document per iteration" in prompt


def test_kb_tool_reflection_prompt_pushes_shortest_path() -> None:
    config = Config()
    config.agents.capability_profile = CapabilityProfile.KNOWLEDGE_BASE
    builder = ContextBuilder(Path("."), config=config)

    prompt = builder.build_tool_reflection_prompt()

    assert "Choose the shortest next step." in prompt
    assert 'target_uri="viking://resources/"' in prompt
    assert "call the next retrieval tool directly instead of replying with a prose-only plan." in prompt
    assert "Avoid long OR/boolean expansions unless the first focused query fails." in prompt


def test_bid_material_initial_search_prompt_uses_high_level_tools() -> None:
    config = Config()
    config.agents.capability_profile = CapabilityProfile.BID_MATERIAL
    builder = ContextBuilder(Path("."), config=config)

    prompt = builder.build_retrieval_initial_search_prompt()

    assert "search_certificates" in prompt
    assert "search_solution_materials" in prompt
    assert "collect_bid_evidence" in prompt
