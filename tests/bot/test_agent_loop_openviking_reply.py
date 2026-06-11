# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for grounded OpenViking image-section replies."""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from vikingbot.agent.loop import AgentLoop
from vikingbot.bus.events import InboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.config.schema import Config, SessionKey
from vikingbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from vikingbot.session.manager import SessionManager


class StubProvider(LLMProvider):
    """Minimal provider stub for grounded-reply tests."""

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


def test_agent_loop_prepares_text_block_for_rewrite() -> None:
    cleaned = AgentLoop._prepare_text_block_for_rewrite(
        """
**1.****        ****场景和需求**

1）打开浏览器，在地址栏输入相应的网址，进入XMS界面。

2）在该界面的右下角有一个问号的图标，单击该图标，打开『帮助说明』。
""".strip()
    )

    assert cleaned.startswith("1. 场景和需求")
    assert "1. 打开浏览器" in cleaned
    assert "2. 在该界面的右下角有一个问号的图标" in cleaned
    assert "**" not in cleaned


def test_semantic_section_selector_trusts_model_for_business_alias() -> None:
    config = Config()
    provider = StubProvider([LLMResponse(content='{"sections":[2]}')])

    with tempfile.TemporaryDirectory() as tmpdir:
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=Path(tmpdir),
            config=config,
        )
        blocks = asyncio.run(
            loop._collect_document_evidence_blocks_semantic(
                "宾客有哪些状态",
                [
                    {
                        "tool_name": "openviking_read",
                        "args": (
                            '{"uri": "viking://resources/xms-support/01-base/01-base_2.md", '
                            '"level": "read"}'
                        ),
                        "result": """
1.1工号
工号是操作员编号。

1.2主单代码
R 预订
I 在住
Q 问询
""".strip(),
                        "execute_success": True,
                    }
                ],
                SessionKey(type="cli", channel_id="default", chat_id="semantic-alias"),
            )
        )

    assert blocks == ["1.2主单代码\nR 预订\nI 在住\nQ 问询"]
    assert provider.calls[-1]["session_id"].endswith(":kb-section-select")
    prompt = provider.calls[-1]["messages"][-1]["content"]
    assert "宾客有哪些状态" in prompt
    assert "1.2主单代码" in prompt


def test_semantic_section_selector_respects_empty_selection_without_local_rescue() -> None:
    config = Config()
    provider = StubProvider([LLMResponse(content='{"sections":[]}')])

    with tempfile.TemporaryDirectory() as tmpdir:
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=Path(tmpdir),
            config=config,
        )
        blocks = asyncio.run(
            loop._collect_document_evidence_blocks_semantic(
                "宾客有哪些状态",
                [
                    {
                        "tool_name": "openviking_read",
                        "args": (
                            '{"uri": "viking://resources/xms-support/01-base/01-base_1.md", '
                            '"level": "read"}'
                        ),
                        "result": """
1.1宾客偏好
维护宾客喜欢的房型、楼层和备注。

1.2系统状态
系统状态用于判断服务是否在线。
""".strip(),
                        "execute_success": True,
                    }
                ],
                SessionKey(type="cli", channel_id="default", chat_id="semantic-empty"),
            )
        )

    assert blocks == []


def test_semantic_section_selector_returns_empty_on_unusable_model_output() -> None:
    config = Config()
    provider = StubProvider([LLMResponse(content="not json")])

    with tempfile.TemporaryDirectory() as tmpdir:
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=Path(tmpdir),
            config=config,
        )
        blocks = asyncio.run(
            loop._collect_document_evidence_blocks_semantic(
                "宾客有哪些状态",
                [
                    {
                        "tool_name": "openviking_read",
                        "args": (
                            '{"uri": "viking://resources/xms-support/01-base/01-base_2.md", '
                            '"level": "read"}'
                        ),
                        "result": """
2.1宾客状态
R 预订状态
I 当前在住
Q 问询状态
""".strip(),
                        "execute_success": True,
                    }
                ],
                SessionKey(type="cli", channel_id="default", chat_id="semantic-invalid-output"),
            )
        )

    assert blocks == []


def test_extract_user_text_uses_latest_user_message_for_followup() -> None:
    assert (
        AgentLoop._extract_user_text(
            [
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "宾客有哪些状态"},
                {"role": "assistant", "content": "宾客状态包括 R、I、Q。"},
                {"role": "user", "content": "那客房状态呢？"},
            ]
        )
        == "那客房状态呢？"
    )


def test_semantic_evidence_query_uses_model_rewrite_for_short_followup() -> None:
    config = Config()
    provider = StubProvider([LLMResponse(content="客房状态有哪些？")])

    with tempfile.TemporaryDirectory() as tmpdir:
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=Path(tmpdir),
            config=config,
        )
        query = asyncio.run(
            loop._build_semantic_evidence_query(
                [
                    {"role": "system", "content": "system prompt"},
                    {"role": "user", "content": "宾客有哪些状态"},
                    {"role": "assistant", "content": "宾客状态包括 R、I、Q。"},
                    {"role": "user", "content": "那客房呢？"},
                ],
                SessionKey(type="cli", channel_id="default", chat_id="query-rewrite"),
            )
        )

    assert query == "客房状态有哪些？"
    assert provider.calls[-1]["session_id"].endswith(":kb-query-rewrite")


def test_semantic_evidence_query_keeps_latest_text_when_rewrite_fails() -> None:
    class FailingProvider(StubProvider):
        async def chat(self, *args, **kwargs):
            raise RuntimeError("rewrite unavailable")

    config = Config()

    with tempfile.TemporaryDirectory() as tmpdir:
        loop = AgentLoop(
            bus=MessageBus(),
            provider=FailingProvider([]),
            workspace=Path(tmpdir),
            config=config,
        )
        query = asyncio.run(
            loop._build_semantic_evidence_query(
                [
                    {"role": "system", "content": "system prompt"},
                    {"role": "user", "content": "宾客有哪些状态"},
                    {"role": "assistant", "content": "宾客状态包括 R、I、Q。"},
                    {"role": "user", "content": "那客房呢？"},
                ],
                SessionKey(type="cli", channel_id="default", chat_id="query-rewrite-fail"),
            )
        )

    assert query == "那客房呢？"


def test_finalize_kb_response_ignores_previous_turn_document_evidence() -> None:
    config = Config()
    provider = StubProvider([])

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
        )

        messages = [
            {"role": "user", "content": "宾客有哪些状态"},
            {
                "role": "assistant",
                "content": "读取宾客状态",
                "tool_calls": [
                    {
                        "id": "old-read",
                        "type": "function",
                        "function": {
                            "name": "openviking_read",
                            "arguments": (
                                '{"uri": "viking://resources/xms-support/01-base/01-base_2.md", '
                                '"level": "read"}'
                            ),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "old-read",
                "name": "openviking_read",
                "content": "2.1宾客状态\nR 预订状态\nI 当前在住\nQ 问询状态",
            },
            {"role": "assistant", "content": "宾客状态包括 R、I、Q。"},
            {"role": "user", "content": "那客房状态呢？"},
        ]

        final_content = asyncio.run(
            loop._finalize_kb_response(
                "当前资料不足以回答客房状态。",
                SessionKey(type="cli", channel_id="default", chat_id="kb-followup"),
                messages=messages,
            )
        )

    assert final_content == "当前资料不足以回答客房状态。"
    assert "参考文档" not in final_content
    assert provider.calls == []


def test_finalize_kb_response_does_not_rewrite_text_without_current_turn_document_blocks() -> None:
    config = Config()
    provider = StubProvider([])

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
        )

        final_content = asyncio.run(
            loop._finalize_kb_response(
                "已从 01-base_2.md 读取文档，正在整理最终答案。",
                SessionKey(type="cli", channel_id="default", chat_id="kb-final-clean"),
                messages=[{"role": "user", "content": "宾客有哪些状态"}],
            )
        )

    assert final_content == "已从 01-base_2.md 读取文档，正在整理最终答案。"
    assert provider.calls == []


def test_run_agent_loop_separates_kb_draft_from_final_user_reply() -> None:
    config = Config()

    provider = StubProvider(
        [
            LLMResponse(
                content="先读取文档确认细节。",
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="openviking_read",
                        arguments={
                            "uri": "viking://resources/xms-support/01-base.docx/01-base_2.md",
                            "level": "read",
                        },
                        tokens=12,
                    )
                ],
            ),
            LLMResponse(content='{"sections":[1],"coverage":"full","missing":"","next_query":""}'),
            LLMResponse(content="宾客状态包括 R、I、O、D、H、N、S、X、Q。"),
        ]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。\n\n"
            "## Final Answer Contract\n- Final user reply must not mention internal file names.\n",
            encoding="utf-8",
        )
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=config,
            max_iterations=3,
        )
        loop.tools.get_definitions = lambda: []
        loop.tools.execute = AsyncMock(
            return_value="""
2.1宾客状态
主单状态反映一个客人的信息在酒店中所处的状态。
R 预订状态
I 当前在住
O 本日结账
D 昨日结账
H 已结账
N 应到未到的预订
S 临时挂账
X 已被取消的预订
Q 问询状态
""".strip()
        )

        final_content, tools_used, _token_usage, _iteration = asyncio.run(
            loop._run_agent_loop(
                messages=[{"role": "user", "content": "宾客有哪些状态"}],
                session_key=SessionKey(type="cli", channel_id="default", chat_id="kb-final"),
                publish_events=False,
            )
        )

    assert len(tools_used) == 1
    assert final_content.startswith("宾客状态包括 R、I、O、D、H、N、S、X、Q。")
    assert "参考文档" in final_content
    assert "/bot/v1/resources/preview?uri=" in final_content
    answer_body = final_content.split("参考文档", 1)[0]
    assert "01-base_2.md" not in answer_body
    assert "SOUL.md" not in final_content
    assert any(
        isinstance(message.get("content"), str)
        and "Relevant document evidence for the current user request" in message["content"]
        for message in provider.calls[-1]["messages"]
    )
    assert provider.calls[-1]["tools"] == []


def test_run_agent_loop_can_answer_after_semantic_evidence_while_tools_remain_available() -> None:
    config = Config()
    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="openviking_read",
                        arguments={
                            "uri": "viking://resources/demo/status.md",
                            "level": "read",
                        },
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(content='{"sections":[1],"coverage":"full","missing":"","next_query":""}'),
            LLMResponse(content="Code C means the component is queued."),
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
            max_iterations=3,
        )
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": "openviking_read",
                    "description": "Read docs",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        loop.tools.get_definitions = lambda: tool_defs
        loop.tools.execute = AsyncMock(return_value="Status C means the component is queued.")

        final_content, tools_used, _token_usage, _iteration = asyncio.run(
            loop._run_agent_loop(
                messages=[{"role": "user", "content": "What does status C mean?"}],
                session_key=SessionKey(type="cli", channel_id="default", chat_id="answer-only"),
                publish_events=False,
            )
        )

    assert final_content.startswith("Code C means the component is queued.")
    assert [tool["tool_name"] for tool in tools_used] == ["openviking_read"]
    assert provider.calls[0]["tools"] == tool_defs
    assert provider.calls[-1]["tools"] == tool_defs
    assert any(
        isinstance(message.get("content"), str)
        and "separate semantic coverage assessment" in message["content"]
        for message in provider.calls[-1]["messages"]
    )


def test_run_agent_loop_respects_structured_image_request() -> None:
    config = Config()
    image_line = "![入住按钮](send://check-in.png)"
    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="openviking_read",
                        arguments={
                            "uri": "viking://resources/demo/check-in.md",
                            "level": "read",
                            "include_images": True,
                        },
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(content='{"sections":[1],"coverage":"full","missing":"","next_query":""}'),
            LLMResponse(content="打开宾客主单，核对信息后点击【入住】按钮。"),
            LLMResponse(content="1"),
            LLMResponse(content=f"打开宾客主单，核对信息后点击【入住】按钮。\n\n{image_line}"),
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
                    "name": "openviking_read",
                    "description": "Read docs",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        loop.tools.execute = AsyncMock(
            return_value=(
                f"## 单间房入住\n打开宾客主单，核对信息后点击【入住】按钮。\n\n{image_line}"
            )
        )

        final_content, tools_used, _token_usage, _iteration = asyncio.run(
            loop._run_agent_loop(
                messages=[{"role": "user", "content": "如何办理入住？"}],
                session_key=SessionKey(type="cli", channel_id="default", chat_id="image-auto"),
                publish_events=False,
            )
        )

    executed_arguments = loop.tools.execute.await_args.args[1]
    assert executed_arguments["include_images"] is True
    assert image_line in final_content
    assert [tool["tool_name"] for tool in tools_used] == ["openviking_read"]
    assert provider.calls[-2]["session_id"].endswith(":kb-image-select")
    assert provider.calls[-1]["session_id"].endswith(":kb-final")


def test_iteration_limit_answers_from_selected_evidence() -> None:
    config = Config()
    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="openviking_read",
                        arguments={
                            "uri": "viking://resources/demo/status.md",
                            "level": "read",
                        },
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(content='{"sections":[1],"coverage":"full","missing":"","next_query":""}'),
            LLMResponse(content=None),
            LLMResponse(content="Code C means the component is queued."),
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
                    "name": "openviking_read",
                    "description": "Read docs",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
        loop.tools.execute = AsyncMock(return_value="Status C means the component is queued.")

        final_content, tools_used, _token_usage, iteration = asyncio.run(
            loop._run_agent_loop(
                messages=[{"role": "user", "content": "Explain status C"}],
                session_key=SessionKey(
                    type="cli",
                    channel_id="default",
                    chat_id="iteration-limit-evidence",
                ),
                publish_events=False,
            )
        )

    assert iteration == 2
    assert final_content.startswith("Code C means the component is queued.")
    assert [tool["tool_name"] for tool in tools_used] == ["openviking_read"]
    assert provider.calls[-1]["session_id"].endswith(":kb-selected-evidence-answer")


def test_run_agent_loop_executes_more_retrieval_after_partial_semantic_evidence() -> None:
    config = Config()
    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="openviking_read",
                        arguments={
                            "uri": "viking://resources/demo/status.md",
                            "level": "read",
                        },
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(
                content=(
                    '{"sections":[1],"coverage":"partial",'
                    '"missing":"status lifecycle and handling details",'
                    '"next_query":"status C lifecycle handling"}'
                )
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_2",
                        name="openviking_search",
                        arguments={"query": "more status docs"},
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_3",
                        name="openviking_read",
                        arguments={
                            "uri": "viking://resources/demo/status-details.md",
                            "level": "read",
                        },
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(content='{"sections":[1],"coverage":"full","missing":"","next_query":""}'),
            LLMResponse(content="Code C means the component is queued."),
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
            max_iterations=4,
        )
        loop.tools.get_definitions = lambda: [
            {
                "type": "function",
                "function": {
                    "name": "openviking_read",
                    "description": "Read docs",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "openviking_search",
                    "description": "Search docs",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]
        loop.tools.execute = AsyncMock(
            side_effect=[
                "Status C means the component is queued.",
                "OpenViking search query: status C lifecycle handling\nTotal matches: 1",
                "Status C means the component is queued until the scheduler starts it.",
            ]
        )

        final_content, tools_used, _token_usage, iteration = asyncio.run(
            loop._run_agent_loop(
                messages=[{"role": "user", "content": "Explain status C"}],
                session_key=SessionKey(
                    type="cli",
                    channel_id="default",
                    chat_id="ignore-late-tools",
                ),
                publish_events=False,
            )
        )

    assert iteration == 4
    assert final_content.startswith("Code C means the component is queued.")
    assert [tool["tool_name"] for tool in tools_used] == [
        "openviking_read",
        "openviking_search",
        "openviking_read",
    ]
    assert loop.tools.execute.await_count == 3
    assert provider.calls[2]["tools"]
    assert provider.calls[-1]["session_id"].endswith("ignore-late-tools")


def test_run_agent_loop_continues_search_until_concrete_kb_evidence_is_ready() -> None:
    config = Config()

    provider = StubProvider(
        [
            LLMResponse(content="当前文档中似乎没有明确答案。"),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="openviking_search",
                        arguments={"query": "宾客有哪些状态", "target_uri": "viking://resources/"},
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_2",
                        name="openviking_glob",
                        arguments={"pattern": "**/*.md", "uri": "viking://resources/"},
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_3",
                        name="openviking_read",
                        arguments={
                            "uri": "viking://resources/xms-support/01-base/01-base_1.md",
                            "level": "read",
                        },
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(content='{"sections":[1],"coverage":"full","missing":"","next_query":""}'),
            LLMResponse(content="宾客状态包括 R、I、O、D、H、N、S、X、Q。"),
        ]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。\n\n"
            "## Final Answer Contract\n- Final user reply must be based on documentation evidence.\n",
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
        loop.tools.execute = AsyncMock(
            side_effect=[
                (
                    "OpenViking search query: 宾客有哪些状态\n"
                    "Target URI: viking://resources/\n"
                    "Total matches: 1\n\n"
                    "Resources:\n"
                    "1. [document] viking://resources/.abstract.md\n"
                    "   Generic scope summary only. Not a concrete document.\n"
                ),
                "Found 1 file:\n📄 viking://resources/xms-support/01-base/01-base_1.md",
                """
**2.1宾客状态**

主单状态反映一个客人的信息在酒店中所处的状态。
R 预订状态
I 当前在住
O 本日结账
D 昨日结账
H 已结账
N 应到未到的预订
S 临时挂账
X 已被取消的预订
Q 问询状态
""".strip(),
            ]
        )

        final_content, tools_used, _token_usage, _iteration = asyncio.run(
            loop._run_agent_loop(
                messages=[{"role": "user", "content": "宾客有哪些状态"}],
                session_key=SessionKey(type="cli", channel_id="default", chat_id="kb-continue"),
                publish_events=False,
            )
        )

    assert final_content.startswith("宾客状态包括 R、I、O、D、H、N、S、X、Q。")
    assert "参考文档" in final_content
    assert "/bot/v1/resources/preview?uri=" in final_content
    assert [tool["tool_name"] for tool in tools_used] == [
        "openviking_search",
        "openviking_glob",
        "openviking_read",
    ]
    assert any(
        isinstance(message.get("content"), str)
        and "The current evidence is still insufficient." in message["content"]
        for call in provider.calls
        for message in call["messages"]
    )


def test_run_agent_loop_rejects_unrelated_concrete_read_before_answering() -> None:
    config = Config()

    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="openviking_read",
                        arguments={
                            "uri": "viking://resources/xms-support/01-base/01-base_1.md",
                            "level": "read",
                        },
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(content='{"sections":[],"coverage":"none","missing":"","next_query":""}'),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_2",
                        name="openviking_grep",
                        arguments={
                            "uri": "viking://resources/产品手册/",
                            "pattern": "宾客状态",
                            "case_insensitive": True,
                        },
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_3",
                        name="openviking_read",
                        arguments={
                            "uri": "viking://resources/xms-support/01-base/01-base_2.md",
                            "level": "read",
                        },
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(content='{"sections":[1],"coverage":"full","missing":"","next_query":""}'),
            LLMResponse(content="宾客状态包括 R、I、Q。"),
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
            max_iterations=4,
        )
        loop.tools.get_definitions = lambda: []
        loop.tools.execute = AsyncMock(
            side_effect=[
                """
**系统基础**

● 工号：是操作员的编号。
● 密码：密码长度为 0 至 16 位。
● 主单：本系统中把散客、团体的登记单称为主单。
""".strip(),
                (
                    "Found 1 match across 1 pattern:\n\n"
                    "📄 viking://resources/xms-support/01-base/01-base_2.md\n"
                    "   Line 1 (pattern: '宾客状态'):\n"
                    "   **2.1宾客状态**"
                ),
                """
**2.1宾客状态**

主单状态反映一个客人的信息在酒店中所处的状态。
R 预订状态
I 当前在住
Q 问询状态
""".strip(),
            ]
        )

        final_content, tools_used, _token_usage, _iteration = asyncio.run(
            loop._run_agent_loop(
                messages=[{"role": "user", "content": "宾客有哪些状态"}],
                session_key=SessionKey(type="cli", channel_id="default", chat_id="kb-relevant"),
                publish_events=False,
            )
        )

    assert final_content.startswith("宾客状态包括 R、I、Q。")
    assert [tool["tool_name"] for tool in tools_used] == [
        "openviking_read",
        "openviking_grep",
        "openviking_read",
    ]
    assert any(
        isinstance(message.get("content"), str)
        and "openviking_read_no_relevant_section_for_user_request" in message["content"]
        for call in provider.calls
        for message in call["messages"]
    )


def test_process_message_prefetches_kb_search_and_read_before_first_answer() -> None:
    config = Config()

    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="route_1",
                        name="route_request",
                        arguments={
                            "label": "knowledge_query",
                            "route": "agent",
                            "confidence": "high",
                            "reason": "asks about xms login workflow",
                        },
                        tokens=10,
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="openviking_search",
                        arguments={"query": "授权登陆", "target_uri": "viking://resources/"},
                        tokens=8,
                    ),
                    ToolCallRequest(
                        id="call_2",
                        name="openviking_read",
                        arguments={
                            "uri": "viking://resources/xms-support/01-base.docx/01-base_2.md",
                            "level": "read",
                        },
                        tokens=8,
                    ),
                ],
            ),
            LLMResponse(content='{"sections":[1],"coverage":"full","missing":"","next_query":""}'),
            LLMResponse(content="授权登录时，先输入域名或IP，再填写工号、密码并选择酒店和模块。"),
        ]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。\n\n"
            "## Final Answer Contract\n- Final user reply must be based on documentation evidence.\n",
            encoding="utf-8",
        )

        class StubSandboxManager:
            def __init__(self, workspace_path: Path):
                self.config = SimpleNamespace(mode="isolated")
                self.workspace = workspace_path

            def to_workspace_id(self, _session_key):
                return "workspace-1"

            def get_workspace_path(self, _session_key):
                return workspace

            async def get_sandbox(self, _session_key):
                return None

            async def get_sandbox_cwd(self, _session_key):
                return str(workspace)

        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=config,
            max_iterations=2,
            session_manager=SessionManager(workspace / "bot-data"),
            sandbox_manager=StubSandboxManager(workspace),
        )
        loop.tools.get_definitions = lambda: [
            {
                "type": "function",
                "function": {
                    "name": "openviking_search",
                    "description": "Search docs",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "openviking_read",
                    "description": "Read docs",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]
        loop.tools.execute = AsyncMock(
            side_effect=[
                (
                    "OpenViking search query: 授权登陆\n"
                    "Total matches: 2\n\n"
                    "Resources:\n"
                    "1. [document] viking://resources/xms-support/01-base.docx/01-base_2.md\n"
                    "   Content preview omitted. Use openviking_read for evidence.\n"
                    "2. [document] viking://resources/xms-support/01-base.docx/01-base_1.md\n"
                    "   Content preview omitted. Use openviking_read for evidence.\n"
                ),
                (
                    "3.1授权登录\n"
                    "在浏览器中输入系统的域名或 IP 地址，然后输入工号、密码，选择酒店和模块。"
                ),
            ]
        )

        response = asyncio.run(
            loop._process_message(
                InboundMessage(
                    sender_id="user-1",
                    content="如何进行授权登陆?",
                    session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user-1"),
                )
            )
        )

    assert response is not None
    assert response.content.startswith(
        "授权登录时，先输入域名或IP，再填写工号、密码并选择酒店和模块。"
    )
    assert "参考文档" in response.content
    assert "/bot/v1/resources/preview?uri=" in response.content
    assert loop.tools.execute.await_count == 2
    assert loop.tools.execute.await_args_list[0].args[0] == "openviking_search"
    assert loop.tools.execute.await_args_list[1].args[0] == "openviking_read"

    first_agent_messages = next(
        call["messages"]
        for call in provider.calls
        if call["session_id"] == "dingtalk__bot__user-1"
        and any(
            message.get("role") == "tool" and "3.1授权登录" in message.get("content", "")
            for message in call["messages"]
        )
    )
    assert any(
        message.get("role") == "tool" and "3.1授权登录" in message.get("content", "")
        for message in first_agent_messages
    )
    assert any(
        message.get("role") == "assistant"
        and any(
            tool_call["function"]["name"] == "openviking_search"
            for tool_call in message.get("tool_calls", [])
        )
        for message in first_agent_messages
    )


def test_process_message_reuses_structured_grounded_history_without_retrieval() -> None:
    config = Config()
    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="route_1",
                        name="route_request",
                        arguments={
                            "label": "knowledge_query",
                            "route": "agent",
                            "confidence": "high",
                            "reason": "documented status question",
                        },
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="history_answer_1",
                        name=AgentLoop.GROUNDED_HISTORY_ANSWER_TOOL,
                        arguments={"answer": "S 表示临时挂账。"},
                        tokens=8,
                    )
                ],
            ),
        ]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。",
            encoding="utf-8",
        )
        session_key = SessionKey(type="dingtalk", channel_id="bot", chat_id="history-user")
        session_manager = SessionManager(workspace / "bot-data")
        session = session_manager.get_or_create(session_key)
        session.add_message("user", "S是什么状态？")
        session.add_message(
            "assistant",
            "S 表示临时挂账。",
            tools_used=[
                {
                    "tool_name": "openviking_read",
                    "args": '{"uri":"viking://resources/xms/status.md","level":"read"}',
                    "result": "S 临时挂账",
                    "execute_success": True,
                }
            ],
        )
        asyncio.run(session_manager.save(session))

        class StubSandboxManager:
            def __init__(self, workspace_path: Path):
                self.config = SimpleNamespace(mode="isolated")
                self.workspace = workspace_path

            def to_workspace_id(self, _session_key):
                return "workspace-history"

            def get_workspace_path(self, _session_key):
                return workspace

            async def get_sandbox(self, _session_key):
                return None

            async def get_sandbox_cwd(self, _session_key):
                return str(workspace)

        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=config,
            max_iterations=2,
            session_manager=session_manager,
            sandbox_manager=StubSandboxManager(workspace),
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
        loop.tools.execute = AsyncMock()

        response = asyncio.run(
            loop._process_message(
                InboundMessage(
                    sender_id="user-1",
                    content="S是什么状态？",
                    session_key=session_key,
                )
            )
        )

    assert response is not None
    assert response.content == "S 表示临时挂账。"
    assert response.iteration == 1
    assert loop.tools.execute.await_count == 0
    agent_call = provider.calls[1]
    assert agent_call["tool_choice"] == "required"
    assert any(
        tool["function"]["name"] == AgentLoop.GROUNDED_HISTORY_ANSWER_TOOL
        for tool in agent_call["tools"]
    )


def test_detailed_followup_keeps_retrieving_when_prior_document_only_defines_term() -> None:
    config = Config()
    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="read_status",
                        name="openviking_read",
                        arguments={
                            "uri": "viking://resources/xms/status.md",
                            "level": "read",
                        },
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(content="详细介绍 XMS 宾客状态 S"),
            LLMResponse(
                content=(
                    '{"sections":[1],"coverage":"partial",'
                    '"missing":"状态 S 的业务流转、触发条件和处理方式",'
                    '"next_query":"XMS 状态 S 临时挂账 业务流转 处理方式"}'
                )
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="search_status_details",
                        name="openviking_search",
                        arguments={
                            "query": "XMS 状态 S 临时挂账 业务流转 处理方式",
                            "target_uri": "viking://resources/",
                        },
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(content="现有文档只说明 S 表示临时挂账，未提供更详细的业务说明。"),
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
        loop.tools.get_definitions = lambda: [
            {
                "type": "function",
                "function": {
                    "name": "openviking_read",
                    "description": "Read docs",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "openviking_search",
                    "description": "Search docs",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ]
        loop.tools.execute = AsyncMock(
            side_effect=[
                "2.1 宾客状态\nS 临时挂账",
                (
                    "OpenViking search query: XMS 状态 S 临时挂账 业务流转 处理方式\n"
                    "Target URI: viking://resources/\nTotal matches: 0"
                ),
            ]
        )

        final_content, tools_used, _token_usage, iteration = asyncio.run(
            loop._run_agent_loop(
                messages=[
                    {"role": "user", "content": "S 是什么状态"},
                    {"role": "assistant", "content": "S 表示临时挂账。"},
                    {"role": "user", "content": "详细向我介绍状态 S"},
                ],
                session_key=SessionKey(
                    type="cli",
                    channel_id="default",
                    chat_id="detailed-status-followup",
                ),
                publish_events=False,
                allow_grounded_history_reuse=True,
            )
        )

    assert iteration == 2
    assert final_content.startswith("现有文档只说明 S 表示临时挂账")
    assert [tool["tool_name"] for tool in tools_used] == [
        "openviking_read",
        "openviking_search",
    ]
    second_agent_call = provider.calls[3]
    assert second_agent_call["tool_choice"] == "required"
    assert any(
        isinstance(message.get("content"), str)
        and "evidence is relevant but does not fully cover" in message["content"]
        and "viking://resources/xms/status.md" in message["content"]
        for message in second_agent_call["messages"]
    )
