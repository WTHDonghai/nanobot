# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for OpenAPI stream event timing."""

import asyncio
import json
import tempfile
from contextlib import suppress
from pathlib import Path

import pytest
from fastapi import FastAPI

from vikingbot.agent.loop import AgentLoop
from vikingbot.bus.events import InboundMessage, OutboundEventType, OutboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.channels.openapi import OpenAPIChannel, OpenAPIChannelConfig, PendingResponse
from vikingbot.cli.commands import prepare_channel
from vikingbot.channels.openapi_models import ChatRequest, EventType
from vikingbot.config.schema import CapabilityProfile, Config, SessionKey
from vikingbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class StubProvider(LLMProvider):
    """Minimal provider stub for event-ordering tests."""

    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.calls = []

    async def chat(
        self,
        messages,
        tools=None,
        tool_choice=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        session_id=None,
    ):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "session_id": session_id,
            }
        )
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "stub-model"


def _decode_sse_chunk(chunk: str | bytes) -> dict:
    """Parse one SSE data line into JSON."""
    text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
    assert text.startswith("data: ")
    return json.loads(text[len("data: ") :].strip())


@pytest.mark.asyncio
async def test_chat_stream_emits_immediate_progress_event() -> None:
    bus = MessageBus()
    channel = OpenAPIChannel(
        config=OpenAPIChannelConfig(),
        bus=bus,
        workspace_path=Path.cwd(),
    )

    response = await channel._handle_chat_stream(
        ChatRequest(message="如何查看预订信息", session_id="stream-test", user_id="user-1")
    )

    first_chunk = await response.body_iterator.__anext__()
    first_event = _decode_sse_chunk(first_chunk)

    assert first_event["event"] == "reasoning"
    assert first_event["data"] == "Request received. Preparing context..."

    await response.body_iterator.aclose()


def test_prepare_channel_registers_dynamic_openapi_config_for_agent_loop() -> None:
    config = Config()
    config.channels = []

    prepare_channel(
        config,
        MessageBus(),
        fastapi_app=FastAPI(),
        enable_openapi=True,
        openapi_port=18790,
    )

    channel_configs = config.channels_config.get_all_channels()
    assert any(
        isinstance(channel_config, OpenAPIChannelConfig)
        and channel_config.channel_key() == "cli__default"
        and channel_config.max_concurrent_requests == 100
        for channel_config in channel_configs
    )


@pytest.mark.asyncio
async def test_openapi_channel_forwards_iteration_events() -> None:
    bus = MessageBus()
    channel = OpenAPIChannel(
        config=OpenAPIChannelConfig(),
        bus=bus,
        workspace_path=Path.cwd(),
    )
    pending = PendingResponse()
    channel._pending["session-1"] = pending

    await channel.send(
        OutboundMessage(
            session_key=SessionKey(type="cli", channel_id="default", chat_id="session-1"),
            content="Iteration 1/50",
            event_type=OutboundEventType.ITERATION,
        )
    )

    event = await pending.stream_queue.get()
    assert event.event == EventType.ITERATION
    assert event.data == "Iteration 1/50"


@pytest.mark.asyncio
async def test_agent_loop_publishes_tool_call_before_execution_starts() -> None:
    config = Config()
    config.agents.capability_profile = CapabilityProfile.FULL

    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="tool-1",
                        name="openviking_search",
                        arguments={"query": "预订信息"},
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(content="done"),
        ]
    )

    order: list[tuple[str, str]] = []
    bus = MessageBus()

    async def record_outbound(msg: OutboundMessage) -> None:
        order.append(("publish", msg.event_type.value))

    bus.publish_outbound = record_outbound

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=workspace,
            config=config,
            max_iterations=2,
        )
        loop.tools.get_definitions = lambda: []

        async def execute_tool(name, arguments, **kwargs):
            order.append(("execute", name))
            return "search-result"

        loop.tools.execute = execute_tool

        final_content, tools_used, token_usage, iteration = await loop._run_agent_loop(
            messages=[{"role": "user", "content": "帮我查预订信息"}],
            session_key=SessionKey(type="cli", channel_id="default", chat_id="event-order"),
            publish_events=True,
            sender_id="user-1",
        )

    assert final_content == "done"
    assert tools_used[0]["tool_name"] == "openviking_search"
    assert token_usage["total_tokens"] == 0
    assert iteration == 2

    reasoning_index = order.index(("publish", "reasoning"))
    tool_call_index = order.index(("publish", "tool_call"))
    execute_index = order.index(("execute", "openviking_search"))
    tool_result_index = order.index(("publish", "tool_result"))

    assert reasoning_index < tool_call_index < execute_index < tool_result_index


@pytest.mark.asyncio
async def test_agent_loop_publishes_kb_text_draft_as_reasoning_before_retry() -> None:
    config = Config()
    config.agents.capability_profile = CapabilityProfile.KNOWLEDGE_BASE

    provider = StubProvider(
        [
            LLMResponse(content="我先检索宾客状态相关文档。"),
            LLMResponse(content="第二轮依然没有拿到文档证据。"),
        ]
    )

    published: list[tuple[str, str]] = []
    bus = MessageBus()

    async def record_outbound(msg: OutboundMessage) -> None:
        published.append((msg.event_type.value, msg.content))

    bus.publish_outbound = record_outbound

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=workspace,
            config=config,
            max_iterations=2,
        )
        loop.tools.get_definitions = lambda: []

        final_content, tools_used, token_usage, iteration = await loop._run_agent_loop(
            messages=[{"role": "user", "content": "帮我查宾客状态"}],
            session_key=SessionKey(type="cli", channel_id="default", chat_id="kb-text-draft"),
            publish_events=True,
            sender_id="user-1",
        )

    assert (
        final_content
        == "抱歉，我暂时没有在现有支持资料中找到足够依据来回答这个问题。需要的话，我可以帮您转人工继续跟进，您看需要吗？"
    )
    assert tools_used == []
    assert token_usage["total_tokens"] == 0
    assert iteration == 2
    assert ("reasoning", "我先检索宾客状态相关文档。") in published


@pytest.mark.asyncio
async def test_agent_loop_requires_tool_call_on_first_kb_iteration() -> None:
    config = Config()
    config.agents.capability_profile = CapabilityProfile.KNOWLEDGE_BASE

    provider = StubProvider([LLMResponse(content="Reached by forced tool-choice test.")])

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=config,
            max_iterations=1,
        )
        loop.tools.get_definitions = lambda: [
            {
                "type": "function",
                "function": {
                    "name": "openviking_search",
                    "description": "Search docs",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]

        final_content, tools_used, token_usage, iteration = await loop._run_agent_loop(
            messages=[{"role": "user", "content": "入住后可以提现吗"}],
            session_key=SessionKey(type="cli", channel_id="default", chat_id="kb-required"),
            publish_events=False,
            sender_id="user-1",
        )

    assert (
        final_content
        == "抱歉，我暂时没有在现有支持资料中找到足够依据来回答这个问题。需要的话，我可以帮您转人工继续跟进，您看需要吗？"
    )
    assert tools_used == []
    assert token_usage["total_tokens"] == 0
    assert iteration == 1
    assert provider.calls[0]["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_agent_loop_processes_different_sessions_concurrently() -> None:
    config = Config()
    config.channels = [OpenAPIChannelConfig(max_concurrent_requests=2)]

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        loop = AgentLoop(
            bus=MessageBus(),
            provider=StubProvider([]),
            workspace=workspace,
            config=config,
        )

        first_started = asyncio.Event()
        second_started = asyncio.Event()
        allow_first_to_finish = asyncio.Event()

        async def fake_process(msg: InboundMessage) -> OutboundMessage:
            if msg.session_key.chat_id == "session-1":
                first_started.set()
                await allow_first_to_finish.wait()
            else:
                second_started.set()
                allow_first_to_finish.set()

            return OutboundMessage(session_key=msg.session_key, content=f"done:{msg.session_key.chat_id}")

        loop._process_message = fake_process  # type: ignore[method-assign]
        runner = asyncio.create_task(loop.run())

        try:
            await loop.bus.publish_inbound(
                InboundMessage(
                    sender_id="user-1",
                    content="first",
                    session_key=SessionKey(type="cli", channel_id="default", chat_id="session-1"),
                )
            )
            await asyncio.wait_for(first_started.wait(), timeout=1.0)

            await loop.bus.publish_inbound(
                InboundMessage(
                    sender_id="user-2",
                    content="second",
                    session_key=SessionKey(type="cli", channel_id="default", chat_id="session-2"),
                )
            )
            await asyncio.wait_for(second_started.wait(), timeout=1.0)

            results = {
                (await asyncio.wait_for(loop.bus.consume_outbound(), timeout=1.0)).content,
                (await asyncio.wait_for(loop.bus.consume_outbound(), timeout=1.0)).content,
            }
        finally:
            loop.stop()
            runner.cancel()
            with suppress(asyncio.CancelledError):
                await runner

    assert results == {"done:session-1", "done:session-2"}


@pytest.mark.asyncio
async def test_agent_loop_keeps_same_session_messages_serialized() -> None:
    config = Config()
    config.channels = [OpenAPIChannelConfig(max_concurrent_requests=2)]

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        loop = AgentLoop(
            bus=MessageBus(),
            provider=StubProvider([]),
            workspace=workspace,
            config=config,
        )

        first_started = asyncio.Event()
        second_started = asyncio.Event()
        allow_first_to_finish = asyncio.Event()

        async def fake_process(msg: InboundMessage) -> OutboundMessage:
            if msg.metadata.get("seq") == 1:
                first_started.set()
                await allow_first_to_finish.wait()
            else:
                second_started.set()

            return OutboundMessage(session_key=msg.session_key, content=f"done:{msg.metadata.get('seq')}")

        loop._process_message = fake_process  # type: ignore[method-assign]
        runner = asyncio.create_task(loop.run())

        try:
            session_key = SessionKey(type="cli", channel_id="default", chat_id="same-session")
            await loop.bus.publish_inbound(
                InboundMessage(
                    sender_id="user-1",
                    content="first",
                    session_key=session_key,
                    metadata={"seq": 1},
                )
            )
            await asyncio.wait_for(first_started.wait(), timeout=1.0)

            await loop.bus.publish_inbound(
                InboundMessage(
                    sender_id="user-1",
                    content="second",
                    session_key=session_key,
                    metadata={"seq": 2},
                )
            )

            await asyncio.sleep(0.1)
            assert not second_started.is_set()

            allow_first_to_finish.set()
            await asyncio.wait_for(second_started.wait(), timeout=1.0)

            results = [
                (await asyncio.wait_for(loop.bus.consume_outbound(), timeout=1.0)).content,
                (await asyncio.wait_for(loop.bus.consume_outbound(), timeout=1.0)).content,
            ]
        finally:
            loop.stop()
            runner.cancel()
            with suppress(asyncio.CancelledError):
                await runner

    assert results == ["done:1", "done:2"]
