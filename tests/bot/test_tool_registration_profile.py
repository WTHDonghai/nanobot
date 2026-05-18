# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for capability-profile-based tool registration."""

from vikingbot.agent.tools.factory import register_default_tools
from vikingbot.agent.tools.ov_file import VikingSearchTool
from vikingbot.agent.tools.registry import ToolRegistry
from vikingbot.config.schema import CapabilityProfile, Config


def test_register_default_tools_knowledge_base_profile_only_keeps_openviking_qa_tools() -> None:
    registry = ToolRegistry()
    config = Config()
    config.agents.capability_profile = CapabilityProfile.KNOWLEDGE_BASE

    register_default_tools(
        registry=registry,
        config=config,
        send_callback=None,
        subagent_manager=None,
        cron_service=None,
    )

    assert set(registry.tool_names) == {
        "openviking_read",
        "openviking_list",
        "openviking_search",
        "openviking_grep",
        "openviking_glob",
    }


def test_register_default_tools_technical_support_profile_uses_openviking_qa_tools() -> None:
    registry = ToolRegistry()
    config = Config()
    config.agents.capability_profile = CapabilityProfile.TECHNICAL_SUPPORT

    register_default_tools(
        registry=registry,
        config=config,
        send_callback=None,
        subagent_manager=None,
        cron_service=None,
    )

    assert set(registry.tool_names) == {
        "openviking_read",
        "openviking_list",
        "openviking_search",
        "openviking_grep",
        "openviking_glob",
    }


def test_register_default_tools_full_profile_includes_human_handoff() -> None:
    registry = ToolRegistry()
    config = Config()
    config.agents.capability_profile = CapabilityProfile.FULL

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
    )

    assert "human_handoff" in registry.tool_names


def test_register_default_tools_bid_material_profile_registers_high_level_tools() -> None:
    registry = ToolRegistry()
    config = Config()
    config.agents.capability_profile = CapabilityProfile.BID_MATERIAL

    register_default_tools(
        registry=registry,
        config=config,
        send_callback=None,
        subagent_manager=None,
        cron_service=None,
    )

    assert set(registry.tool_names) == {
        "openviking_read",
        "openviking_list",
        "openviking_search",
        "openviking_grep",
        "openviking_glob",
        "search_certificates",
        "search_solution_materials",
        "collect_bid_evidence",
        "human_handoff",
    }


def test_openviking_search_tool_stays_domain_neutral() -> None:
    tool = VikingSearchTool()

    assert "certificate" not in tool.description.lower()
    assert "license" not in tool.description.lower()
    assert tool._is_image_focused_query("证书图片") is True
    assert tool._is_image_focused_query("资质证书") is False
