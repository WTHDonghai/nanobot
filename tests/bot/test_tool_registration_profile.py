# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for explicit agent-mode tool registration."""

from pathlib import Path

import pytest
from vikingbot.agent.context import ContextBuilder
from vikingbot.agent.loop import AgentLoop
from vikingbot.agent.tools.factory import register_default_tools
from vikingbot.agent.tools.ov_file import VikingSearchTool
from vikingbot.agent.tools.registry import ToolRegistry
from vikingbot.bus.queue import MessageBus
from vikingbot.config.schema import AgentMode, Config
from vikingbot.providers.base import LLMProvider


class StubProvider(LLMProvider):
    async def chat(self, *args, **kwargs):
        raise AssertionError("not used")

    def get_default_model(self) -> str:
        return "stub-model"


def test_explicit_knowledge_base_mode_keeps_openviking_qa_tools(tmp_path: Path) -> None:
    (tmp_path / "SOUL.md").write_text(
        "我是知识库助手。回答知识库相关问题前，必须先从知识库获取文档依据。",
        encoding="utf-8",
    )
    config = Config()
    loop = AgentLoop(
        bus=MessageBus(),
        provider=StubProvider(),
        workspace=tmp_path,
        config=config,
    )

    assert loop.context._is_retrieval_mode() is True
    assert set(loop.tools.tool_names) == {
        "openviking_read",
        "openviking_list",
        "openviking_search",
        "openviking_grep",
        "openviking_glob",
    }


def test_full_mode_is_not_inferred_from_workspace_prompt(
    tmp_path: Path,
) -> None:
    (tmp_path / "SOUL.md").write_text(
        "You are a general assistant. Help with local files and code tasks.",
        encoding="utf-8",
    )
    config = Config()
    config.agents.mode = AgentMode.FULL
    builder = ContextBuilder(tmp_path, config=config)

    assert builder._is_retrieval_mode() is False

    registry = ToolRegistry()
    register_default_tools(
        registry=registry,
        config=config,
        send_callback=None,
        subagent_manager=None,
        cron_service=None,
        include_image_tool=False,
        knowledge_base_mode=builder._is_knowledge_base_mode(),
    )

    assert "read_file" in registry.tool_names
    assert "exec" in registry.tool_names


def test_default_mode_stays_restricted_even_with_general_prompt(tmp_path: Path) -> None:
    (tmp_path / "SOUL.md").write_text("You are a general assistant.", encoding="utf-8")
    loop = AgentLoop(
        bus=MessageBus(),
        provider=StubProvider(),
        workspace=tmp_path,
        config=Config(),
    )

    assert loop.context._is_retrieval_mode() is True
    assert "exec" not in loop.tools.tool_names


def test_legacy_full_profile_maps_to_explicit_full_mode() -> None:
    config = Config.model_validate({"agents": {"capability_profile": "full"}})

    assert config.agents.mode == AgentMode.FULL
    assert not hasattr(config.agents, "capability_profile")


def test_unknown_legacy_profile_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported legacy capability profile"):
        Config.model_validate({"agents": {"capability_profile": "unexpected"}})


def test_register_default_tools_knowledge_base_override_keeps_openviking_qa_tools() -> None:
    registry = ToolRegistry()
    config = Config()

    register_default_tools(
        registry=registry,
        config=config,
        send_callback=None,
        subagent_manager=None,
        cron_service=None,
        knowledge_base_mode=True,
    )

    assert set(registry.tool_names) == {
        "openviking_read",
        "openviking_list",
        "openviking_search",
        "openviking_grep",
        "openviking_glob",
    }


def test_register_default_tools_without_kb_prompt_registers_general_tools() -> None:
    registry = ToolRegistry()
    config = Config()

    register_default_tools(
        registry=registry,
        config=config,
        send_callback=None,
        subagent_manager=None,
        cron_service=None,
        include_message_tool=False,
        include_spawn_tool=False,
        include_cron_tool=False,
        include_image_tool=False,
        knowledge_base_mode=False,
    )

    assert "openviking_search" in registry.tool_names
    assert "read_file" in registry.tool_names
    assert "exec" in registry.tool_names
    assert "human_handoff" in registry.tool_names


def test_openviking_search_tool_stays_domain_neutral() -> None:
    tool = VikingSearchTool()

    assert "certificate" not in tool.description.lower()
    assert "license" not in tool.description.lower()
