# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for guided question suggestions in knowledge-base mode."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

from vikingbot.agent.guided_questions import (
    build_guided_questions,
    create_guided_question_token,
    extract_search_evidence,
    should_attempt_guided_questions,
    verify_guided_question_token,
)
from vikingbot.agent.intent_router import IntentDecision, IntentRoute
from vikingbot.agent.loop import AgentLoop
from vikingbot.bus.events import InboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.config.schema import AgentMode, Config, SessionKey
from vikingbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class StubProvider(LLMProvider):
    """Minimal provider stub for guided question tests."""

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
        **kwargs,
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
                **kwargs,
            }
        )
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "stub-model"


SEARCH_RESULT = """OpenViking search query: 维修电话
Target URI: viking://resources/
Total matches: 1

Documents:
1. [document] viking://resources/support/maintenance.md
   Match reason: 文档包含维修报修电话和报修流程。
   Content preview omitted. Use openviking_read for evidence.
"""


def test_should_attempt_guided_questions_for_short_safe_redirect_only() -> None:
    short_out_of_scope = IntentDecision(
        label="out_of_scope",
        route=IntentRoute.SAFE_REDIRECT,
        confidence="medium",
        reason="too short",
    )
    unsafe = IntentDecision(
        label="unsafe_internal",
        route=IntentRoute.SAFE_REDIRECT,
        confidence="high",
        reason="asks for internals",
    )
    agent = IntentDecision(
        label="knowledge_query",
        route=IntentRoute.AGENT,
        confidence="high",
        reason="clear KB question",
    )

    assert should_attempt_guided_questions("维修电话", short_out_of_scope) is True
    assert should_attempt_guided_questions("维修电话", unsafe) is False
    assert should_attempt_guided_questions("维修电话是多少？", short_out_of_scope) is False
    assert should_attempt_guided_questions("维修电话", agent) is False


def test_extract_search_evidence_reads_documents_and_match_reasons() -> None:
    evidence = extract_search_evidence(SEARCH_RESULT)

    assert len(evidence) == 1
    assert evidence[0].uri == "viking://resources/support/maintenance.md"
    assert evidence[0].match_reason == "文档包含维修报修电话和报修流程。"


def test_guided_question_token_validates_session_and_question() -> None:
    token = create_guided_question_token(
        session_id="session-1",
        canonical_question="维修电话是多少？",
        source_uris=["viking://resources/support/maintenance.md"],
        secret="secret",
        now=1000,
    )

    assert verify_guided_question_token(
        token=token,
        session_id="session-1",
        canonical_question="维修电话是多少？",
        secret="secret",
        now=1001,
    )
    assert not verify_guided_question_token(
        token=token,
        session_id="session-2",
        canonical_question="维修电话是多少？",
        secret="secret",
        now=1001,
    )
    assert not verify_guided_question_token(
        token=token,
        session_id="session-1",
        canonical_question="如何报维修？",
        secret="secret",
        now=1001,
    )


def test_build_guided_questions_validates_router_and_search_evidence() -> None:
    provider = StubProvider(
        [
            LLMResponse(
                content=json.dumps(
                    {
                        "questions": [
                            {
                                "display_text": "维修电话是多少？",
                                "canonical_question": "维修电话是多少？",
                                "source_uris": ["viking://resources/support/maintenance.md"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="route-1",
                        name="route_request",
                        arguments={
                            "label": "knowledge_query",
                            "route": "agent",
                            "confidence": "high",
                            "reason": "asks for a documented maintenance phone number",
                        },
                        tokens=5,
                    )
                ],
            ),
        ]
    )
    search_queries: list[tuple[str, int]] = []

    async def search(query: str, limit: int) -> str:
        search_queries.append((query, limit))
        return SEARCH_RESULT

    questions = asyncio.run(
        build_guided_questions(
            provider=provider,
            generation_model="main-model",
            classifier_model="fast-model",
            user_message="维修电话",
            history=[],
            session_id="session-1",
            search=search,
            token_secret="secret",
        )
    )

    assert [item.display_text for item in questions] == ["维修电话是多少？"]
    assert questions[0].canonical_question == "维修电话是多少？"
    assert questions[0].source_uris == ["viking://resources/support/maintenance.md"]
    assert search_queries == [("维修电话", 5), ("维修电话是多少？", 5)]
    assert verify_guided_question_token(
        token=questions[0].token,
        session_id="session-1",
        canonical_question="维修电话是多少？",
        secret="secret",
    )


def test_agent_loop_validates_guided_question_request_token() -> None:
    config = Config()
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        config.storage_workspace = str(workspace)
        (workspace / "SOUL.md").write_text("You are a knowledge-base assistant.", encoding="utf-8")
        loop = AgentLoop(
            bus=MessageBus(),
            provider=StubProvider([]),
            workspace=workspace,
            config=config,
        )
        session_key = SessionKey(type="cli", channel_id="default", chat_id="guided-session")
        token = create_guided_question_token(
            session_id=session_key.safe_name(),
            canonical_question="维修电话是多少？",
            source_uris=["viking://resources/support/maintenance.md"],
            secret=loop._guided_question_token_secret,
        )

        assert loop._is_verified_guided_question_request(
            InboundMessage(
                sender_id="user-1",
                content="维修电话是多少？",
                session_key=session_key,
                metadata={"guided_question_token": token},
            )
        )
        assert not loop._is_verified_guided_question_request(
            InboundMessage(
                sender_id="user-1",
                content="如何报维修？",
                session_key=session_key,
                metadata={"guided_question_token": token},
            )
        )


def test_agent_loop_persists_guided_question_selection_without_token() -> None:
    metadata = AgentLoop._build_persisted_user_metadata(
        {
            "guided_question_token": "signed-token",
            "guided_question_id": "gq_1",
            "guided_source_uris": ["viking://resources/support/maintenance.md"],
        }
    )

    assert metadata == {
        "guided_question_id": "gq_1",
        "guided_source_uris": ["viking://resources/support/maintenance.md"],
    }


def test_agent_loop_returns_guided_questions_for_short_intercepted_request() -> None:
    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="route-short",
                        name="route_request",
                        arguments={
                            "label": "out_of_scope",
                            "route": "safe_redirect",
                            "confidence": "medium",
                            "reason": "short phrase may need clarification",
                        },
                        tokens=5,
                    )
                ],
            ),
            LLMResponse(
                content=json.dumps(
                    {
                        "questions": [
                            {
                                "display_text": "维修电话是多少？",
                                "canonical_question": "维修电话是多少？",
                                "source_uris": ["viking://resources/support/maintenance.md"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="route-guided",
                        name="route_request",
                        arguments={
                            "label": "knowledge_query",
                            "route": "agent",
                            "confidence": "high",
                            "reason": "validated guided knowledge-base question",
                        },
                        tokens=5,
                    )
                ],
            ),
        ]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        config = Config()
        config.storage_workspace = str(workspace)
        config.agents.mode = AgentMode.KNOWLEDGE_BASE
        config.agents.api_key = "test-key"
        config.agents.api_base = "http://provider.example"
        (workspace / "SOUL.md").write_text(
            "You are a knowledge-base assistant.",
            encoding="utf-8",
        )
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=config,
        )
        loop.tools.execute = AsyncMock(return_value=SEARCH_RESULT)
        session_key = SessionKey(type="cli", channel_id="default", chat_id="guided-chat")

        response = asyncio.run(
            loop._process_message(
                InboundMessage(
                    sender_id="user-1",
                    content="维修电话",
                    session_key=session_key,
                    metadata={"openviking_session_id": "guided-chat"},
                )
            )
        )
        persisted_session = loop.sessions.get_or_create(session_key)

    assert response is not None
    assert response.content == "你可能想问这些，选一个我继续查："
    suggestions = response.metadata["guided_questions"]
    assert len(suggestions) == 1
    assert suggestions[0]["display_text"] == "维修电话是多少？"
    assert suggestions[0]["canonical_question"] == "维修电话是多少？"
    assert suggestions[0]["source_uris"] == ["viking://resources/support/maintenance.md"]
    assert persisted_session.messages[-1]["metadata"]["guided_questions"] == suggestions
    assert loop.tools.execute.await_count == 2
    assert provider.calls[0]["session_id"].endswith("::intent-router")
    assert provider.calls[1]["session_id"].endswith("::guided-question-generation")
    assert provider.calls[2]["session_id"].endswith("::intent-router")
