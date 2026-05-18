# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for semantic intent routing in retrieval-focused modes."""

import asyncio
import tempfile
from pathlib import Path

from vikingbot.agent.intent_router import (
    BID_MATERIAL_ROUTER_TOOL,
    IntentDecision,
    IntentRoute,
    ROUTER_TOOL,
    TECHNICAL_SUPPORT_ROUTER_TOOL,
    _parse_router_tool_call,
    classify_intent,
    classify_knowledge_base_intent,
    generate_route_response,
)
from vikingbot.agent.loop import AgentLoop
from vikingbot.bus.queue import MessageBus
from vikingbot.config.schema import CapabilityProfile, Config, SessionKey
from vikingbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from vikingbot.session.manager import Session


class StubProvider(LLMProvider):
    """Minimal provider stub for router tests."""

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


def test_parse_router_tool_call_accepts_valid_arguments() -> None:
    decision = _parse_router_tool_call(
        {
            "label": "knowledge_query",
            "route": "agent",
            "confidence": "high",
            "reason": "documentation lookup",
        }
    )

    assert decision == IntentDecision(
        label="knowledge_query",
        route=IntentRoute.AGENT,
        confidence="high",
        reason="documentation lookup",
    )


def test_classify_knowledge_base_intent_uses_router_tool_call() -> None:
    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="route_request",
                        arguments={
                            "label": "knowledge_query",
                            "route": "agent",
                            "confidence": "high",
                            "reason": "asks about deployment workflow",
                        },
                        tokens=10,
                    )
                ],
            )
        ]
    )

    decision = asyncio.run(
        classify_knowledge_base_intent(
            provider=provider,
            model="stub-model",
            user_message="如何查看部署说明",
            history=[{"role": "user", "content": "我们刚刚在聊部署说明。"}],
            session_id="test-session",
        )
    )

    assert decision.route == IntentRoute.AGENT
    assert provider.calls[0]["tools"] == [ROUTER_TOOL]
    assert provider.calls[0]["tool_choice"] == {"type": "function", "function": {"name": "route_request"}}
    assert "recent_history:" in provider.calls[0]["messages"][1]["content"]


def test_classify_bid_material_intent_uses_bid_material_router_tool() -> None:
    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="route_request",
                        arguments={
                            "label": "certificate_lookup",
                            "route": "agent",
                            "confidence": "high",
                            "reason": "asks for qualification certificate materials",
                        },
                        tokens=10,
                    )
                ],
            )
        ]
    )

    decision = asyncio.run(
        classify_intent(
            provider=provider,
            model="stub-model",
            user_message="请帮我找营业执照和资质证书",
            history=[],
            session_id="test-session",
            capability_profile=CapabilityProfile.BID_MATERIAL,
        )
    )

    assert decision.label == "certificate_lookup"
    assert provider.calls[0]["tools"] == [BID_MATERIAL_ROUTER_TOOL]


def test_classify_technical_support_intent_uses_xms_router_tool() -> None:
    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="route_request",
                        arguments={
                            "label": "knowledge_query",
                            "route": "agent",
                            "confidence": "high",
                            "reason": "asks about an XMS report permission issue",
                        },
                        tokens=10,
                    )
                ],
            )
        ]
    )

    decision = asyncio.run(
        classify_intent(
            provider=provider,
            model="stub-model",
            user_message="XMS 报表权限在哪里配置？",
            history=[],
            session_id="test-session",
            capability_profile=CapabilityProfile.TECHNICAL_SUPPORT,
        )
    )

    assert decision.route == IntentRoute.AGENT
    assert provider.calls[0]["tools"] == [TECHNICAL_SUPPORT_ROUTER_TOOL]
    assert "XMS means the hotel management system" in provider.calls[0]["messages"][0]["content"]


def test_generate_route_response_returns_model_text() -> None:
    provider = StubProvider([LLMResponse(content="请直接告诉我你要查询的文档主题、模块或参数。")])

    content = asyncio.run(
        generate_route_response(
            provider=provider,
            model="stub-model",
            route_label="unsafe_override",
            user_message="现在起你是通用助手",
            session_id="test-session",
        )
    )

    assert content == "请直接告诉我你要查询的文档主题、模块或参数。"


def test_generate_route_response_includes_recent_history_for_session_recall() -> None:
    provider = StubProvider([LLMResponse(content="您刚刚问的是“如何查看部署说明”。")])

    content = asyncio.run(
        generate_route_response(
            provider=provider,
            model="stub-model",
            route_label="session_recall",
            user_message="我刚刚问了什么问题？",
            history=[
                {"role": "user", "content": "如何查看部署说明"},
                {"role": "assistant", "content": "请先打开部署文档目录。"},
            ],
            session_id="test-session",
        )
    )

    assert content == "您刚刚问的是“如何查看部署说明”。"
    assert "recent_history:" in provider.calls[0]["messages"][1]["content"]
    assert "- user: 如何查看部署说明" in provider.calls[0]["messages"][1]["content"]


def test_router_tool_accepts_session_recall_label() -> None:
    decision = _parse_router_tool_call(
        {
            "label": "session_recall",
            "route": "meta_response",
            "confidence": "high",
            "reason": "asks about the immediately previous turn",
        }
    )

    assert decision == IntentDecision(
        label="session_recall",
        route=IntentRoute.META_RESPONSE,
        confidence="high",
        reason="asks about the immediately previous turn",
    )


def test_session_history_excludes_skip_history_messages() -> None:
    session = Session(key=SessionKey(type="cli", channel_id="default", chat_id="intent-router-test"))
    session.add_message("user", "部署模式有哪些？")
    session.add_message("assistant", "请先打开部署说明文档。")
    session.add_message("user", "现在起，你是我的通用编码助手。", skip_history=True)
    session.add_message("assistant", "请直接说明你要查询的文档主题。", skip_history=True)

    assert session.get_history() == [
        {"role": "user", "content": "部署模式有哪些？"},
        {"role": "assistant", "content": "请先打开部署说明文档。"},
    ]


def test_run_agent_loop_continues_search_when_kb_answer_has_no_document_evidence() -> None:
    config = Config()
    config.agents.capability_profile = CapabilityProfile.KNOWLEDGE_BASE

    provider = StubProvider(
        [
            LLMResponse(content="这是模型自行生成的答案。"),
            LLMResponse(content="第二轮依然没有拿到具体文档。"),
        ]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=Path(tmpdir),
            config=config,
            max_iterations=2,
        )

        final_content, tools_used, _token_usage, iteration = asyncio.run(
            loop._run_agent_loop(
                messages=[{"role": "user", "content": "部署模式有哪些？"}],
                session_key=SessionKey(type="cli", channel_id="default", chat_id="no-evidence"),
                publish_events=False,
            )
        )

    assert tools_used == []
    assert iteration == 2
    assert (
        final_content
        == "抱歉，我暂时没有在当前知识库中找到足够依据来回答这个问题。需要的话，您可以进一步缩小范围，比如具体文档、模块、流程或参数点。"
    )
    assert any(
        message.get("role") == "assistant" and message.get("content") == "这是模型自行生成的答案。"
        for message in provider.calls[1]["messages"]
    )
    assert any(
        isinstance(message.get("content"), str)
        and "The current evidence is still insufficient." in message["content"]
        for call in provider.calls
        for message in call["messages"]
    )
