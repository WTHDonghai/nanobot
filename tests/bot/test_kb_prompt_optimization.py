# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for KB prompt optimizations that reduce search drift and token waste."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
from vikingbot.agent.context import ContextBuilder
from vikingbot.agent.loop import AgentLoop
from vikingbot.agent.memory import MemoryStore
from vikingbot.bus.queue import MessageBus
from vikingbot.config.schema import AgentMode, Config, SessionKey
from vikingbot.providers.base import LLMProvider, LLMResponse


class StubProvider(LLMProvider):
    """Minimal provider stub for finalizer tests."""

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
        raise AssertionError("finalizer should not call provider on the fast path")

    def get_default_model(self) -> str:
        return "stub-model"


class SequenceProvider(LLMProvider):
    """Provider stub that returns a fixed sequence and records calls."""

    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self.responses = list(responses)
        self.calls: list[dict] = []

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


class FakeSandboxManager:
    def to_workspace_id(self, session_key: SessionKey) -> str:
        return "workspace-a"

    async def get_sandbox_cwd(self, session_key: SessionKey) -> str:
        return "/workspace"


class FakeMemoryStore:
    def __init__(
        self,
        agent_memory: str = "",
        delay_seconds: float = 0.0,
        agent_error: Exception | None = None,
    ) -> None:
        self.agent_memory = agent_memory
        self.delay_seconds = delay_seconds
        self.agent_error = agent_error
        self.user_memory_calls = 0
        self.agent_memory_calls = 0

    async def get_viking_user_profile(self, workspace_id: str, user_id: str) -> str:
        return ""

    async def get_viking_memory_context(
        self, current_message: str, workspace_id: str, sender_id: str
    ) -> str:
        self.user_memory_calls += 1
        return "<memory>User memory should only appear in full mode.</memory>"

    async def get_viking_agent_memory_context(
        self,
        current_message: str,
        *,
        agent_id: str | None = None,
        owner_user_id: str | None = None,
    ) -> str:
        self.agent_memory_calls += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.agent_error:
            raise self.agent_error
        assert agent_id == "default"
        assert owner_user_id == "default"
        return self.agent_memory


def test_kb_initial_search_prompt_prefers_focused_resource_scoped_lookup() -> None:
    config = Config()
    builder = ContextBuilder(Path("."), config=config)
    prompt = builder.build_kb_initial_search_prompt()

    assert 'target_uri="viking://resources/"' in prompt
    assert "call retrieval tools directly instead of replying with a prose-only plan." in prompt
    assert "Avoid long OR/boolean query expansions on the first search." in prompt
    assert "Prefer 1 search + 1 read before deciding to broaden." in prompt


def test_kb_continue_search_prompt_reads_concrete_doc_before_new_search() -> None:
    config = Config()
    builder = ContextBuilder(Path("."), config=config)
    prompt = builder.build_kb_continue_search_prompt("Current search state")

    assert 'Stay in target_uri="viking://resources/"' in prompt
    assert "If a concrete document URI is already available, read it before any new search." in prompt
    assert "Emit the next retrieval tool call directly." in prompt
    assert "Usually inspect one new document per iteration" in prompt


def test_kb_tool_reflection_prompt_pushes_shortest_path() -> None:
    config = Config()
    workspace = Path(".")
    builder = ContextBuilder(workspace, config=config)

    prompt = builder.build_tool_reflection_prompt()

    assert "Choose the shortest next step." in prompt
    assert 'target_uri="viking://resources/"' in prompt
    assert "call the next retrieval tool directly instead of replying with a prose-only plan." in prompt
    assert "Avoid long OR/boolean expansions unless the first focused query fails." in prompt


def test_default_tool_reflection_prompt_stays_general_in_full_mode() -> None:
    config = Config()
    config.agents.mode = AgentMode.FULL
    builder = ContextBuilder(Path("."), config=config)

    prompt = builder.build_tool_reflection_prompt()

    assert prompt == "Reflect on the results and decide next steps."
    assert "Default search scope" not in prompt


def test_explicit_default_controls_knowledge_base_mode_without_legacy_profile(tmp_path: Path) -> None:
    (tmp_path / "TOOLS.md").write_text(
        "**IMPORTANT: Use the internal document repository as the only evidence source for user-facing answers.**",
        encoding="utf-8",
    )
    config = Config()
    builder = ContextBuilder(tmp_path, config=config)

    assert not hasattr(config.agents, "capability_profile")
    assert builder._is_knowledge_base_mode() is True
    assert "For this knowledge-base request" in builder.build_retrieval_initial_search_prompt()


def test_retrieval_final_response_prompt_forbids_unsupported_facts(tmp_path: Path) -> None:
    config = Config()
    (tmp_path / "SOUL.md").write_text(
        "我是知识库助手。回答问题必须基于当前知识库中的文档依据。",
        encoding="utf-8",
    )
    builder = ContextBuilder(tmp_path, config=config)

    prompt = builder.build_retrieval_final_response_system_prompt()

    assert "Every factual statement must be explicitly supported by the provided evidence." in prompt
    assert "Do not use model knowledge to complete missing parts." in prompt
    assert "Do not add unstated details" in prompt
    assert "answer only the supported part" in prompt


def test_knowledge_base_messages_include_agent_memory_hints_not_user_memory(
    tmp_path: Path,
) -> None:
    async def run_case() -> tuple[list[dict], FakeMemoryStore, dict]:
        config = Config()
        builder = ContextBuilder(
            tmp_path,
            sandbox_manager=FakeSandboxManager(),
            sender_id="guest-user",
            config=config,
        )
        memory = FakeMemoryStore(
            "<memory><abstract>Use 报修 as the focused search keyword.</abstract></memory>"
        )
        builder._memory = memory

        messages = await builder.build_messages(
            history=[],
            current_message="报修要准备什么信息？",
            session_key=SessionKey(type="web", channel_id="guest", chat_id="chat-1"),
        )
        return messages, memory, builder.agent_memory_read_stats

    messages, memory, stats = asyncio.run(run_case())
    contents = [str(message.get("content", "")) for message in messages]
    joined = "\n".join(contents)

    assert memory.agent_memory_calls == 1
    assert memory.user_memory_calls == 0
    assert "Agent memory hints from helpful feedback" in joined
    assert "not factual evidence" in joined
    assert "Use 报修 as the focused search keyword." in joined
    assert "You do not need to use tool to search again" not in joined
    assert "For this knowledge-base request" in contents[-1]
    assert stats["status"] == "ok"
    assert stats["result_count"] == 0
    assert stats["duration_ms"] >= 0


def test_knowledge_base_agent_memory_timeout_skips_hints(tmp_path: Path) -> None:
    async def run_case() -> tuple[list[dict], FakeMemoryStore, dict]:
        config = Config()
        config.ov_server.agent_memory_read_timeout_ms = 1
        builder = ContextBuilder(
            tmp_path,
            sandbox_manager=FakeSandboxManager(),
            sender_id="guest-user",
            config=config,
        )
        memory = FakeMemoryStore(
            "<memory><abstract>This should be skipped on timeout.</abstract></memory>",
            delay_seconds=0.2,
        )
        builder._memory = memory

        messages = await builder.build_messages(
            history=[],
            current_message="报修要准备什么信息？",
            session_key=SessionKey(type="web", channel_id="guest", chat_id="chat-timeout"),
        )
        return messages, memory, builder.agent_memory_read_stats

    messages, memory, stats = asyncio.run(run_case())
    joined = "\n".join(str(message.get("content", "")) for message in messages)

    assert memory.agent_memory_calls == 1
    assert stats["status"] == "timeout"
    assert stats["timeout_ms"] == 100
    assert stats["duration_ms"] >= 0
    assert "Agent memory hints from helpful feedback" not in joined
    assert "This should be skipped on timeout." not in joined
    assert "For this knowledge-base request" in str(messages[-1].get("content", ""))


def test_knowledge_base_agent_memory_error_is_tracked_not_empty(tmp_path: Path) -> None:
    async def run_case() -> tuple[list[dict], FakeMemoryStore, dict]:
        config = Config()
        builder = ContextBuilder(
            tmp_path,
            sandbox_manager=FakeSandboxManager(),
            sender_id="guest-user",
            config=config,
        )
        memory = FakeMemoryStore(agent_error=RuntimeError("memory backend unavailable"))
        builder._memory = memory

        messages = await builder.build_messages(
            history=[],
            current_message="报修要准备什么信息？",
            session_key=SessionKey(type="web", channel_id="guest", chat_id="chat-error"),
        )
        return messages, memory, builder.agent_memory_read_stats

    messages, memory, stats = asyncio.run(run_case())
    joined = "\n".join(str(message.get("content", "")) for message in messages)

    assert memory.agent_memory_calls == 1
    assert stats["status"] == "error"
    assert stats["duration_ms"] >= 0
    assert "Agent memory hints from helpful feedback" not in joined
    assert "For this knowledge-base request" in str(messages[-1].get("content", ""))


def test_agent_memory_store_propagates_lookup_errors(tmp_path: Path, monkeypatch) -> None:
    async def fail_create(*args, **kwargs):
        raise RuntimeError("memory backend unavailable")

    monkeypatch.setattr("vikingbot.agent.memory.VikingClient.create", fail_create)
    store = MemoryStore(tmp_path)

    with pytest.raises(RuntimeError, match="memory backend unavailable"):
        asyncio.run(
            store.get_viking_agent_memory_context(
                "报修",
                agent_id="support-agent",
                owner_user_id="support-owner",
            )
        )


def test_retrieval_finalizer_appends_reference_document_link_for_knowledge_base_mode() -> None:
    async def run_case(workspace: Path) -> str:
        config = Config()
        loop = AgentLoop(
            bus=MessageBus(),
            provider=StubProvider(),
            workspace=workspace,
            config=config,
        )
        uri = "viking://resources/XMS/8.3酒店EDP维护手册(XMS)/01-base_2.md"
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-read-1",
                        "type": "function",
                        "function": {
                            "name": "openviking_read",
                            "arguments": json.dumps(
                                {"uri": uri, "level": "read"}, ensure_ascii=False
                            ),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-read-1",
                "name": "openviking_read",
                "content": "宾客状态包含若干代码说明。",
            },
        ]

        return await loop._finalize_kb_response(
            "XMS 宾客状态包含若干代码说明。",
            SessionKey(type="cli", channel_id="default", chat_id="reference-test"),
            messages,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。",
            encoding="utf-8",
        )
        reply = asyncio.run(run_case(workspace))
    assert reply.startswith("XMS 宾客状态包含若干代码说明。")
    assert "参考文档" in reply
    assert "8.3酒店EDP维护手册(XMS) / 01 base 2" in reply
    assert "/bot/v1/resources/preview?uri=" in reply
    assert "&token=" in reply


def test_retrieval_finalizer_references_only_semantically_selected_sources() -> None:
    async def run_case(workspace: Path) -> str:
        config = Config()
        loop = AgentLoop(
            bus=MessageBus(),
            provider=StubProvider(),
            workspace=workspace,
            config=config,
        )
        selected_uri = "viking://resources/demo/selected.md"
        unrelated_uri = "viking://resources/demo/unrelated.md"
        messages = [
            {"role": "user", "content": "Explain code C"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "selected-read",
                        "type": "function",
                        "function": {
                            "name": "openviking_read",
                            "arguments": json.dumps(
                                {"uri": selected_uri, "level": "read"}, ensure_ascii=False
                            ),
                        },
                    },
                    {
                        "id": "unrelated-read",
                        "type": "function",
                        "function": {
                            "name": "openviking_read",
                            "arguments": json.dumps(
                                {"uri": unrelated_uri, "level": "read"}, ensure_ascii=False
                            ),
                        },
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "selected-read",
                "name": "openviking_read",
                "content": "Code C means queued.",
            },
            {
                "role": "tool",
                "tool_call_id": "unrelated-read",
                "name": "openviking_read",
                "content": "Unrelated settings.",
            },
            {
                "role": "system",
                "content": loop._build_relevant_evidence_prompt(
                    "Explain code C",
                    ["Code C means queued."],
                    source_uri=selected_uri,
                ),
            },
        ]

        return await loop._finalize_kb_response(
            "Code C means queued.",
            SessionKey(type="cli", channel_id="default", chat_id="selected-reference-test"),
            messages,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。",
            encoding="utf-8",
        )
        reply = asyncio.run(run_case(workspace))

    assert "selected" in reply
    assert "unrelated" not in reply


def test_retrieval_finalizer_can_decline_images_for_plain_text_draft() -> None:
    async def run_case(workspace: Path) -> tuple[str, SequenceProvider]:
        config = Config()
        provider = SequenceProvider([LLMResponse(content="NONE")])
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=config,
        )
        uri = "viking://resources/demo/component-codes.md"
        evidence_block = "Code C means the component is currently queued."
        image_evidence_block = f"{evidence_block}\n\n![Code C screenshot](send://code-c.png)"
        messages = [
            {"role": "user", "content": "What does code C mean?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-read-1",
                        "type": "function",
                        "function": {
                            "name": "openviking_read",
                            "arguments": json.dumps(
                                {"uri": uri, "level": "read"}, ensure_ascii=False
                            ),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-read-1",
                "name": "openviking_read",
                "content": image_evidence_block,
            },
            {
                "role": "system",
                "content": loop._build_relevant_evidence_prompt(
                    "What does code C mean?",
                    [image_evidence_block],
                    source_uri=uri,
                ),
            },
        ]

        return (
            await loop._finalize_kb_response(
                "Code C means the component is currently queued.",
                SessionKey(type="cli", channel_id="default", chat_id="plain-text-finalizer-test"),
                messages,
            ),
            provider,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。",
            encoding="utf-8",
        )
        reply, provider = asyncio.run(run_case(workspace))

    assert reply.startswith("Code C means the component is currently queued.")
    assert "send://code-c.png" not in reply
    assert "参考文档" in reply
    assert provider.calls[0]["session_id"].endswith(":kb-image-select")


def test_image_selector_caps_selected_segments() -> None:
    async def run_case(workspace: Path) -> tuple[list[str], SequenceProvider]:
        provider = SequenceProvider([LLMResponse(content="1,2,3,4,5,6")])
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=Config(),
        )
        segments = [
            f"Step {index}\n\n![screen {index}](send://screen-{index}.png)"
            for index in range(1, 7)
        ]
        selected = await loop._select_relevant_image_segments(
            "Follow these steps.",
            segments,
            SessionKey(type="cli", channel_id="default", chat_id="image-cap-test"),
            user_request="How do I complete the workflow?",
        )
        return selected, provider

    with tempfile.TemporaryDirectory() as tmpdir:
        selected, provider = asyncio.run(run_case(Path(tmpdir)))

    assert len(selected) == 4
    assert "send://screen-4.png" in selected[-1]
    assert "send://screen-5.png" not in "\n".join(selected)
    assert "Select at most 4 segments." in provider.calls[0]["messages"][0]["content"]


def test_retrieval_finalizer_can_add_useful_image_to_plain_text_draft() -> None:
    async def run_case(workspace: Path) -> tuple[str, SequenceProvider]:
        config = Config()
        image_line = "![Report expert screen](send://report-expert.png)"
        provider = SequenceProvider(
            [
                LLMResponse(content="1"),
            ]
        )
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=config,
        )
        uri = "viking://resources/demo/report-expert.md"
        evidence_block = (
            "Open Report Expert from the Query menu. The screen shows the report search area."
            f"\n\n{image_line}"
        )
        messages = [
            {"role": "user", "content": "How do I find Report Expert in the UI?"},
            {
                "role": "system",
                "content": loop._build_relevant_evidence_prompt(
                    "How do I find Report Expert in the UI?",
                    [evidence_block],
                    source_uri=uri,
                ),
            },
        ]

        return (
            await loop._finalize_kb_response(
                "Open Report Expert from the Query menu.",
                SessionKey(type="cli", channel_id="default", chat_id="image-choice-test"),
                messages,
            ),
            provider,
        )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。",
            encoding="utf-8",
        )
        reply, provider = asyncio.run(run_case(workspace))

    assert "相关图文说明" in reply
    assert "Open Report Expert from the Query menu. The screen shows the report search area." in reply
    assert "![Report expert screen](send://report-expert.png)" in reply
    assert provider.calls[0]["session_id"].endswith(":kb-image-select")
    assert len(provider.calls) == 1


def test_retrieval_finalizer_inlines_images_when_agent_selects_them() -> None:
    async def run_case(workspace: Path) -> tuple[str, SequenceProvider]:
        image_line = "![入住按钮](send://check-in.png)"
        provider = SequenceProvider([LLMResponse(content="1")])
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=Config(),
        )
        evidence_block = f"单击入住按钮完成登记。\n\n{image_line}"
        messages = [
            {"role": "user", "content": "如何办理入住？"},
            {
                "role": "system",
                "content": loop._build_relevant_evidence_prompt(
                    "如何办理入住？",
                    [evidence_block],
                    source_uri="viking://resources/demo/check-in.md",
                ),
            },
        ]
        session_key = SessionKey(type="cli", channel_id="default", chat_id="inline-image-test")
        reply = await loop._finalize_kb_response(
            "打开宾客主单，核对信息后点击入住按钮。",
            session_key,
            messages,
        )
        return reply, provider

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。",
            encoding="utf-8",
        )
        reply, provider = asyncio.run(run_case(workspace))

    assert "相关图文说明" in reply
    assert "单击入住按钮完成登记。" in reply
    assert "![入住按钮](send://check-in.png)" in reply
    assert len(provider.calls) == 1


def test_retrieval_finalizer_rebuilds_images_from_evidence_not_model_draft() -> None:
    async def run_case(workspace: Path) -> tuple[str, SequenceProvider]:
        image_line = "![image144](send://bd57ec19e4d4434e825864ab8a303223.jpeg)"
        provider = SequenceProvider([LLMResponse(content="1")])
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=Config(),
        )
        evidence_block = (
            "图2.5-2 VIP客人信息列表\n\n"
            "团队：点击【团队】标签，即可查看团队信息列表。\n\n"
            f"{image_line}"
        )
        messages = [
            {"role": "user", "content": "团队信息怎么查看？"},
            {
                "role": "system",
                "content": loop._build_relevant_evidence_prompt(
                    "团队信息怎么查看？",
                    [evidence_block],
                    source_uri="viking://resources/demo/team.md",
                ),
            },
        ]
        bad_draft = (
            "团队：点击【团队】标签，即可查看团队信息列表，默认显示预抵状态下的团队信息，"
            "如图： ](send://bd57ec19e4d4434e825864ab8a303223.jpeg)image144 "
            "图2.5-3 团队信息列表"
        )
        session_key = SessionKey(type="cli", channel_id="default", chat_id="image-rebuild-test")
        reply = await loop._finalize_kb_response(bad_draft, session_key, messages)
        return reply, provider

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "我是知识库助手。回答问题必须基于当前知识库中的文档依据。",
            encoding="utf-8",
        )
        reply, provider = asyncio.run(run_case(workspace))

    assert "团队：点击【团队】标签，即可查看团队信息列表" in reply
    assert "相关图文说明" in reply
    assert "图2.5-2 VIP客人信息列表" in reply
    assert "![image144](send://bd57ec19e4d4434e825864ab8a303223.jpeg)" in reply
    assert "](send://bd57ec19e4d4434e825864ab8a303223.jpeg)image144" not in reply
    assert reply.count("send://bd57ec19e4d4434e825864ab8a303223.jpeg") == 1
    assert len(provider.calls) == 1
