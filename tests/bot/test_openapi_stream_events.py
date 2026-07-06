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
from fastapi.testclient import TestClient
from vikingbot.agent.loop import AgentLoop
from vikingbot.bus.events import InboundMessage, OutboundEventType, OutboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.channels import openapi as openapi_channel
from vikingbot.channels.openapi import OpenAPIChannel, OpenAPIChannelConfig, PendingResponse
from vikingbot.channels.openapi_models import ChatRequest, EventType
from vikingbot.cli.commands import prepare_channel
from vikingbot.config.schema import AgentMode, Config, SessionKey
from vikingbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from vikingbot.utils import set_bot_data_path

from openviking_cli.resource_preview import (
    RESOURCE_PREVIEW_SECRET_ENV,
    create_resource_preview_token,
)

KB_FALLBACK_RESPONSE = (
    "抱歉，我暂时没有在当前知识库中找到足够依据来回答这个问题。需要的话，您可以进一步缩小范围，"
    "比如具体文档、模块、流程或参数点。"
)


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


class StreamingStubProvider(StubProvider):
    """Provider stub that emits response deltas before returning final content."""

    async def chat(
        self,
        messages,
        tools=None,
        tool_choice=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        session_id=None,
        on_delta=None,
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
                "on_delta": on_delta,
            }
        )
        response = self.responses.pop(0)
        if on_delta and session_id and session_id.endswith(":kb-selected-evidence-answer"):
            await on_delta("流")
            await on_delta("式")
        return response


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


def test_openapi_resource_preview_requires_valid_signed_capability(monkeypatch, tmp_path) -> None:
    class StubVikingClient:
        closed = False

        async def stat(self, uri: str):
            return {"name": "01-base_2.md"}

        async def read_content(self, uri: str, level: str = "read"):
            return "宾客状态包含 A、R、D 等代码。\n\n![状态图](viking://resources/XMS/_images/status.png)"

        async def materialize_inline_image_refs(self, content: str, source_uri: str):
            assert source_uri == "viking://resources/XMS/01-base_2.md"
            return content.replace(
                "![状态图](viking://resources/XMS/_images/status.png)",
                "![状态图](send://status.png)",
            )

        async def close(self):
            self.closed = True

    async def create_client():
        return StubVikingClient()

    monkeypatch.setenv(RESOURCE_PREVIEW_SECRET_ENV, "preview-secret")
    monkeypatch.setattr(openapi_channel.VikingClient, "create", create_client)
    set_bot_data_path(tmp_path)
    images_dir = tmp_path / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    (images_dir / "status.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    app = FastAPI()
    channel = OpenAPIChannel(
        config=OpenAPIChannelConfig(),
        bus=MessageBus(),
        workspace_path=Path.cwd(),
        app=app,
    )
    channel._setup_routes()

    client = TestClient(app)
    uri = "viking://resources/XMS/01-base_2.md"
    token = create_resource_preview_token(
        uri=uri,
        account_id="default",
        secret="preview-secret",
    )

    blocked = client.get(
        "/bot/v1/resources/preview",
        params={"uri": uri, "token": "invalid"},
    )
    assert blocked.status_code == 404

    remote_client = TestClient(app, client=("203.0.113.10", 1234))
    mismatched_uri_response = remote_client.get(
        "/bot/v1/resources/preview",
        params={"uri": "viking://resources/XMS/other.md", "token": token},
    )
    assert mismatched_uri_response.status_code == 404

    json_response = client.get(
        "/bot/v1/resources/preview",
        params={"uri": uri, "token": token},
        headers={"Accept": "application/json"},
    )

    assert json_response.status_code == 200
    assert json_response.json() == {
        "title": "01-base_2.md",
        "uri": uri,
        "markdown": "宾客状态包含 A、R、D 等代码。\n\n![状态图](/bot/v1/images/status.png)",
    }

    response = client.get(
        "/bot/v1/resources/preview",
        params={"uri": uri, "token": token},
    )

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "01-base_2.md" in response.text
    assert "宾客状态包含 A、R、D 等代码。" in response.text
    assert "![状态图](/bot/v1/images/status.png)" in response.text

    assert "application/json" in json_response.headers["content-type"]


def test_openapi_resource_preview_rejects_other_account_capability(monkeypatch, tmp_path) -> None:
    create_called = False

    async def create_client():
        nonlocal create_called
        create_called = True
        raise AssertionError("client should not be created")

    monkeypatch.setenv(RESOURCE_PREVIEW_SECRET_ENV, "preview-secret")
    monkeypatch.setattr(openapi_channel.VikingClient, "create", create_client)
    set_bot_data_path(tmp_path)

    app = FastAPI()
    channel = OpenAPIChannel(
        config=OpenAPIChannelConfig(),
        bus=MessageBus(),
        workspace_path=Path.cwd(),
        app=app,
    )
    channel._setup_routes()

    uri = "viking://resources/XMS/remote.md"
    token = create_resource_preview_token(
        uri=uri,
        account_id="another-account",
        secret="preview-secret",
    )
    response = TestClient(app).get(
        "/bot/v1/resources/preview",
        params={"uri": uri, "token": token},
    )

    assert response.status_code == 404
    assert create_called is False


def test_openapi_channel_rewrites_reference_preview_links_with_base_url() -> None:
    channel = OpenAPIChannel(
        config=OpenAPIChannelConfig(base_url="https://support.example.com"),
        bus=MessageBus(),
        workspace_path=Path.cwd(),
    )

    content = "参考文档\n- [01 base 2](/bot/v1/resources/preview?uri=viking%3A%2F%2Fresources%2Fdoc.md)"
    rewritten = channel._replace_bot_resource_links(content)

    assert (
        "[01 base 2](https://support.example.com/bot/v1/resources/preview?uri=viking%3A%2F%2Fresources%2Fdoc.md)"
        in rewritten
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
async def test_openapi_channel_forwards_response_delta_before_final_response() -> None:
    bus = MessageBus()
    channel = OpenAPIChannel(
        config=OpenAPIChannelConfig(),
        bus=bus,
        workspace_path=Path.cwd(),
    )
    pending = PendingResponse()
    channel._pending["session-1"] = pending
    session_key = SessionKey(type="cli", channel_id="default", chat_id="session-1")

    await channel.send(
        OutboundMessage(
            session_key=session_key,
            content="正在",
            event_type=OutboundEventType.RESPONSE_DELTA,
        )
    )
    await channel.send(
        OutboundMessage(
            session_key=session_key,
            content="正在回复",
            event_type=OutboundEventType.RESPONSE,
        )
    )

    delta_event = await pending.stream_queue.get()
    final_event = await pending.stream_queue.get()
    close_event = await pending.stream_queue.get()

    assert delta_event.event == EventType.RESPONSE_DELTA
    assert delta_event.data == "正在"
    assert final_event.event == EventType.RESPONSE
    assert final_event.data == "正在回复"
    assert close_event is None


@pytest.mark.asyncio
async def test_openapi_channel_forwards_guided_question_suggestions() -> None:
    bus = MessageBus()
    channel = OpenAPIChannel(
        config=OpenAPIChannelConfig(),
        bus=bus,
        workspace_path=Path.cwd(),
    )
    pending = PendingResponse()
    channel._pending["session-1"] = pending
    suggestions = [
        {
            "id": "gq_1",
            "display_text": "维修电话是多少？",
            "canonical_question": "维修电话是多少？",
            "token": "signed-token",
            "source_uris": ["viking://resources/support/maintenance.md"],
            "confidence": "high",
        }
    ]

    await channel.send(
        OutboundMessage(
            session_key=SessionKey(type="cli", channel_id="default", chat_id="session-1"),
            content="你可能想问这些，选一个我继续查：",
            event_type=OutboundEventType.RESPONSE,
            metadata={"guided_questions": suggestions},
        )
    )

    final_event = await pending.stream_queue.get()
    suggestions_event = await pending.stream_queue.get()
    close_event = await pending.stream_queue.get()

    assert final_event.event == EventType.RESPONSE
    assert suggestions_event.event == EventType.SUGGESTIONS
    assert suggestions_event.data == suggestions
    assert pending.suggestions == suggestions
    assert close_event is None


@pytest.mark.asyncio
async def test_chat_stream_carries_request_metadata_to_inbound_message() -> None:
    bus = MessageBus()
    channel = OpenAPIChannel(
        config=OpenAPIChannelConfig(),
        bus=bus,
        workspace_path=Path.cwd(),
    )

    response = await channel._handle_chat_stream(
        ChatRequest(
            message="维修电话是多少？",
            session_id="metadata-session",
            user_id="user-1",
            metadata={"guided_question_token": "signed-token"},
        )
    )

    first_chunk = await response.body_iterator.__anext__()
    first_event = _decode_sse_chunk(first_chunk)
    inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1.0)

    assert first_event["event"] == "reasoning"
    assert inbound.content == "维修电话是多少？"
    assert inbound.metadata["guided_question_token"] == "signed-token"
    assert inbound.metadata["openviking_session_id"] == "metadata-session"

    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_agent_loop_publishes_tool_call_before_execution_starts() -> None:
    config = Config()
    config.agents.mode = AgentMode.FULL
    config.agents.api_key = "test-key"
    config.agents.api_base = "http://provider.example"

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
        (workspace / "SOUL.md").write_text(
            "You are a general assistant. Help with local files and code tasks.",
            encoding="utf-8",
        )
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

        final_content, tools_used, token_usage, iteration = await loop._run_agent_loop_classic(
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
async def test_agent_loop_response_delta_helper_respects_publish_flag() -> None:
    config = Config()
    published: list[tuple[str, str]] = []
    bus = MessageBus()

    async def record_outbound(msg: OutboundMessage) -> None:
        published.append((msg.event_type.value, msg.content))

    bus.publish_outbound = record_outbound

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "You are a general assistant.",
            encoding="utf-8",
        )
        loop = AgentLoop(
            bus=bus,
            provider=StubProvider([]),
            workspace=workspace,
            config=config,
            max_iterations=1,
        )
        session_key = SessionKey(type="cli", channel_id="default", chat_id="openapi-delta")

        await loop._publish_response_delta_events(
            session_key=session_key,
            content="实时回复正文。",
            publish_events=True,
        )
    assert any(event_type == "response_delta" for event_type, _ in published)

    published.clear()
    await loop._publish_response_delta_events(
        session_key=session_key,
        content="CLI 回复。",
        publish_events=False,
    )
    assert all(event_type != "response_delta" for event_type, _ in published)


@pytest.mark.asyncio
async def test_agent_loop_response_delta_helper_publishes_only_missing_suffix() -> None:
    config = Config()
    published: list[tuple[str, str]] = []
    bus = MessageBus()

    async def record_outbound(msg: OutboundMessage) -> None:
        published.append((msg.event_type.value, msg.content))

    bus.publish_outbound = record_outbound

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "You are a general assistant.",
            encoding="utf-8",
        )
        loop = AgentLoop(
            bus=bus,
            provider=StubProvider([]),
            workspace=workspace,
            config=config,
            max_iterations=1,
        )
        session_key = SessionKey(type="cli", channel_id="default", chat_id="delta-suffix")
        on_delta = loop._make_response_delta_callback(
            session_key=session_key,
            publish_events=True,
        )
        assert on_delta is not None

        await on_delta("部分")
        await loop._publish_missing_response_delta_events(
            session_key=session_key,
            final_content="部分完整回答",
            publish_events=True,
        )

    assert published == [
        ("response_delta", "部分"),
        ("response_delta", "完整回答"),
    ]


@pytest.mark.asyncio
async def test_agent_loop_response_delta_helper_skips_mismatched_fallback_text() -> None:
    config = Config()
    published: list[tuple[str, str]] = []
    bus = MessageBus()

    async def record_outbound(msg: OutboundMessage) -> None:
        published.append((msg.event_type.value, msg.content))

    bus.publish_outbound = record_outbound

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "You are a general assistant.",
            encoding="utf-8",
        )
        loop = AgentLoop(
            bus=bus,
            provider=StubProvider([]),
            workspace=workspace,
            config=config,
            max_iterations=1,
        )
        session_key = SessionKey(type="cli", channel_id="default", chat_id="delta-mismatch")
        on_delta = loop._make_response_delta_callback(
            session_key=session_key,
            publish_events=True,
        )
        assert on_delta is not None

        await on_delta("旧前缀")
        await loop._publish_missing_response_delta_events(
            session_key=session_key,
            final_content="新答案",
            publish_events=True,
        )

    assert published == [("response_delta", "旧前缀")]


@pytest.mark.asyncio
async def test_agent_loop_streams_provider_deltas_for_final_kb_answer() -> None:
    config = Config()

    provider = StreamingStubProvider(
        [
            LLMResponse(content="流式回答正文"),
        ]
    )
    published: list[tuple[str, str]] = []
    bus = MessageBus()

    async def record_outbound(msg: OutboundMessage) -> None:
        published.append((msg.event_type.value, msg.content))

    bus.publish_outbound = record_outbound

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。",
            encoding="utf-8",
        )
        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=workspace,
            config=config,
            max_iterations=1,
        )
        messages = [
            {"role": "user", "content": "宾客状态有哪些？"},
            {
                "role": "system",
                "content": loop._build_relevant_evidence_prompt(
                    "宾客状态有哪些？",
                    ["## 宾客状态\n宾客状态包含 A、R、D。"],
                    source_uri="viking://resources/demo/status.md",
                ),
            },
        ]
        final_content = await loop._compose_answer_from_selected_evidence(
            messages,
            session_key=SessionKey(type="cli", channel_id="default", chat_id="kb-stream"),
            publish_events=True,
        )

    assert final_content == "流式回答正文"
    assert [event for event, _ in published].count("response_delta") == 2
    assert ("response_delta", "流") in published
    assert ("response_delta", "式") in published


@pytest.mark.asyncio
async def test_agent_loop_publishes_kb_text_draft_as_reasoning_before_retry() -> None:
    config = Config()

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
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。",
            encoding="utf-8",
        )
        loop = AgentLoop(
            bus=bus,
            provider=provider,
            workspace=workspace,
            config=config,
            max_iterations=2,
        )
        loop.tools.get_definitions = lambda: []

        final_content, tools_used, token_usage, iteration = await loop._run_agent_loop_classic(
            messages=[{"role": "user", "content": "帮我查宾客状态"}],
            session_key=SessionKey(type="cli", channel_id="default", chat_id="kb-text-draft"),
            publish_events=True,
            sender_id="user-1",
        )

    assert final_content == KB_FALLBACK_RESPONSE
    assert tools_used == []
    assert token_usage["total_tokens"] == 0
    assert iteration == 2
    assert ("reasoning", "我先检索宾客状态相关文档。") in published


@pytest.mark.asyncio
async def test_agent_loop_requires_tool_call_until_kb_document_evidence_is_ready() -> None:
    config = Config()

    provider = StubProvider(
        [
            LLMResponse(content="First round still lacks document evidence."),
            LLMResponse(content="Second round still lacks document evidence."),
        ]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。",
            encoding="utf-8",
        )
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=config,
            max_iterations=2,
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

        final_content, tools_used, token_usage, iteration = await loop._run_agent_loop_classic(
            messages=[{"role": "user", "content": "入住后可以提现吗"}],
            session_key=SessionKey(type="cli", channel_id="default", chat_id="kb-required"),
            publish_events=False,
            sender_id="user-1",
        )

    assert final_content == KB_FALLBACK_RESPONSE
    assert tools_used == []
    assert token_usage["total_tokens"] == 0
    assert iteration == 2
    assert len(provider.calls) == 2
    assert provider.calls[0]["tool_choice"] == "required"
    assert provider.calls[1]["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_agent_loop_stops_on_provider_error_response() -> None:
    config = Config()
    provider = StubProvider(
        [
            LLMResponse(
                content="Error calling LLM: litellm.BadRequestError: invalid tool_choice",
                finish_reason="error",
            )
        ]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。",
            encoding="utf-8",
        )
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=config,
            max_iterations=5,
        )
        loop.tools.get_definitions = lambda: []

        final_content, tools_used, token_usage, iteration = await loop._run_agent_loop_classic(
            messages=[{"role": "user", "content": "如何办理入住"}],
            session_key=SessionKey(type="cli", channel_id="default", chat_id="provider-error"),
            publish_events=False,
            sender_id="user-1",
        )

    assert final_content == "抱歉，当前模型调用失败，暂时无法完成回答。请检查模型配置或服务端日志后重试。"
    assert tools_used == []
    assert token_usage["total_tokens"] == 0
    assert iteration == 1
    assert len(provider.calls) == 1


def test_agent_loop_classifies_openviking_connection_errors_as_not_evidence() -> None:
    assert (
        AgentLoop._classify_tool_error_result(
            "Error searching Viking: All connection attempts failed"
        )
        == "tool_connection_failed"
    )
    assert (
        AgentLoop._classify_tool_error_result("Error reading from Viking: ConnectError")
        == "tool_connection_failed"
    )
    assert (
        AgentLoop._classify_tool_error_result("Error searching Viking with glob: timeout")
        == "openviking_glob_error"
    )


def test_agent_loop_search_trace_summary_includes_limit_and_total() -> None:
    summary = AgentLoop._summarize_tool_result_for_trace(
        "openviking_search",
        {},
        "\n".join(
            [
                "OpenViking search query: 宾客状态",
                "Target URI: viking://resources/",
                "Requested limit: server default",
                "Total matches: 50",
                "",
                "Documents:",
                "1. [document] viking://resources/demo/宾客状态.md",
                "   Content preview omitted. Use openviking_read for evidence.",
            ]
        ),
    )

    assert "requested_limit=server default" in summary
    assert "total_matches=50" in summary


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
