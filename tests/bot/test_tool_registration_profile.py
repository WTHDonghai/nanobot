# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for capability-profile-based tool registration."""

from vikingbot.agent.tools.factory import register_default_tools
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
