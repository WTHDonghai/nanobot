# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared human handoff service and tool."""

from pathlib import Path

import pytest

from vikingbot.agent.tools.base import ToolContext
from vikingbot.agent.tools.human_handoff import HumanHandoffTool
from vikingbot.bus.queue import MessageBus
from vikingbot.channels.openapi import OpenAPIChannel, OpenAPIChannelConfig
from vikingbot.channels.openapi_models import HumanHandoffRequest
from vikingbot.config.schema import Config, HumanHandoffToolConfig, SessionKey
from vikingbot.services.human_handoff import (
    HumanHandoffPayload,
    HumanHandoffResult,
    HumanHandoffService,
)


class StubHandoffService:
    """Capture handoff payloads and return a canned result."""

    def __init__(self) -> None:
        self.calls: list[HumanHandoffPayload] = []

    async def request_handoff(self, payload: HumanHandoffPayload) -> HumanHandoffResult:
        self.calls.append(payload)
        return HumanHandoffResult(
            success=True,
            status="accepted",
            message="转人工请求已提交。",
            handoff_id="handoff-123",
            entry_url="https://example.com/handoff",
            service_response={"source": payload.source},
        )


@pytest.mark.asyncio
async def test_human_handoff_service_returns_configured_entry_url_without_remote_call() -> None:
    service = HumanHandoffService(
        HumanHandoffToolConfig(
            enabled=True,
            entry_url="https://example.com/handoff",
            service_url="",
        )
    )

    result = await service.request_handoff(
        HumanHandoffPayload(session_id="session-1", user_id="user-1")
    )

    assert result.success is True
    assert result.status == "ready"
    assert result.entry_url == "https://example.com/handoff"


@pytest.mark.asyncio
async def test_human_handoff_tool_uses_session_context_defaults() -> None:
    service = StubHandoffService()
    tool = HumanHandoffTool(service)

    result = await tool.execute(
        ToolContext(
            session_key=SessionKey(type="cli", channel_id="default", chat_id="session-1"),
            sender_id="user-1",
        ),
        reason="user_requested_human_handoff",
        summary="设备问题需要人工跟进",
        latest_user_message="请帮我转人工",
    )

    assert "转人工请求已提交。" in result
    assert "handoff_id=handoff-123" in result
    assert "entry_url=https://example.com/handoff" in result

    payload = service.calls[0]
    assert payload.session_id == "session-1"
    assert payload.user_id == "user-1"
    assert payload.source == "agent_tool"
    assert payload.metadata["channel_type"] == "cli"
    assert payload.metadata["channel_id"] == "default"


@pytest.mark.asyncio
async def test_openapi_channel_handoff_reuses_shared_service() -> None:
    channel = OpenAPIChannel(
        config=OpenAPIChannelConfig(),
        bus=MessageBus(),
        workspace_path=Path.cwd(),
        bot_config=Config(),
    )
    stub_service = StubHandoffService()
    channel._human_handoff_service = stub_service
    channel._sessions["session-1"] = {"user_id": "user-from-session"}

    response = await channel._handle_handoff(
        HumanHandoffRequest(
            session_id="session-1",
            reason="user_requested_human_handoff",
            source="admin_chat_ui",
        )
    )

    assert response.success is True
    assert response.handoff_id == "handoff-123"
    assert response.entry_url == "https://example.com/handoff"

    payload = stub_service.calls[0]
    assert payload.user_id == "user-from-session"
    assert payload.source == "admin_chat_ui"
    assert payload.metadata["channel_type"] == "cli"
    assert payload.metadata["channel_id"] == "default"
