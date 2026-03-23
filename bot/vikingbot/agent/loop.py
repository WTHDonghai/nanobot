"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from vikingbot.agent.context import ContextBuilder
from vikingbot.agent.intent_router import (
    IntentRoute,
    classify_knowledge_base_intent,
    generate_route_response,
)
from vikingbot.agent.memory import MemoryStore
from vikingbot.agent.subagent import SubagentManager
from vikingbot.agent.tools import register_default_tools
from vikingbot.agent.tools.registry import ToolRegistry
from vikingbot.bus.events import InboundMessage, OutboundEventType, OutboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.config import load_config
from vikingbot.config.schema import BotMode, CapabilityProfile, Config, SessionKey
from vikingbot.hooks import HookContext
from vikingbot.hooks.manager import hook_manager
from vikingbot.providers.base import LLMProvider
from vikingbot.sandbox import SandboxManager
from vikingbot.session.manager import SessionManager
from vikingbot.utils.helpers import cal_str_tokens
from vikingbot.utils.tracing import trace

if TYPE_CHECKING:
    from vikingbot.config.schema import ExecToolConfig
    from vikingbot.cron.service import CronService


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 50,
        memory_window: int = 50,
        brave_api_key: str | None = None,
        exa_api_key: str | None = None,
        gen_image_model: str | None = None,
        exec_config: "ExecToolConfig | None" = None,
        cron_service: "CronService | None" = None,
        session_manager: SessionManager | None = None,
        sandbox_manager: SandboxManager | None = None,
        config: Config = None,
        eval: bool = False,
    ):
        """
        Initialize the AgentLoop with all required dependencies and configuration.

        Args:
            bus: MessageBus instance for publishing and subscribing to messages.
            provider: LLMProvider instance for making LLM calls.
            workspace: Path to the workspace directory for file operations.
            model: Optional model identifier. If not provided, uses the provider's default.
            max_iterations: Maximum number of tool execution iterations per message (default: 50).
            memory_window: Maximum number of messages to keep in session memory (default: 50).
            brave_api_key: Optional API key for Brave search integration.
            exa_api_key: Optional API key for Exa search integration.
            gen_image_model: Optional model identifier for image generation (default: openai/doubao-seedream-4-5-251128).
            exec_config: Optional configuration for the exec tool (command execution).
            cron_service: Optional CronService for scheduled task management.
            session_manager: Optional SessionManager for session persistence. If not provided, a new one is created.
            sandbox_manager: Optional SandboxManager for sandboxed operations.
            config: Optional Config object with full configuration. Used if other parameters are not provided.

        Note:
            The AgentLoop creates its own ContextBuilder, SessionManager (if not provided),
            ToolRegistry, and SubagentManager during initialization.

        Example:
            >>> loop = AgentLoop(
            ...     bus=message_bus,
            ...     provider=llm_provider,
            ...     workspace=Path("/path/to/workspace"),
            ...     model="gpt-4",
            ...     max_iterations=30,
            ... )
        """
        from vikingbot.config.schema import ExecToolConfig  # noqa: F811

        self.bus = bus
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.memory_window = memory_window
        self.brave_api_key = brave_api_key
        self.exa_api_key = exa_api_key
        self.gen_image_model = gen_image_model or "openai/doubao-seedream-4-5-251128"
        self.exec_config = exec_config or ExecToolConfig()
        self.cron_service = cron_service
        self.sandbox_manager = sandbox_manager
        self.config = config

        self.context = ContextBuilder(
            workspace,
            sandbox_manager=sandbox_manager,
            config=self.config,
        )

        self._register_builtin_hooks()
        self.sessions = session_manager or SessionManager(
            self.config.bot_data_path, sandbox_manager=sandbox_manager
        )
        self.tools = ToolRegistry()
        self._eval = eval
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            config=self.config,
            model=self.model,
            sandbox_manager=sandbox_manager,
        )

        self._running = False
        self._register_default_tools()

    async def _publish_thinking_event(
        self, session_key: SessionKey, event_type: OutboundEventType, content: str
    ) -> None:
        """
        Publish a thinking event to the message bus.

        Thinking events are used to communicate the agent's internal processing
        state to the user, such as when the agent is executing a tool or
        processing a complex request.

        Args:
            session_key: The session key identifying the conversation.
            event_type: The type of thinking event (e.g., THINKING, TOOL_START).
            content: The message content to display to the user.

        Note:
            This is an internal method used by the agent loop to communicate
            progress to users during long-running operations.

        Example:
            >>> await self._publish_thinking_event(
            ...     session_key=SessionKey(channel="telegram", chat_id="123"),
            ...     event_type=OutboundEventType.TOOL_START,
            ...     content="Executing web search..."
            ... )
        """
        await self.bus.publish_outbound(
            OutboundMessage(
                session_key=session_key,
                content=content,
                event_type=event_type,
            )
        )

    def _register_builtin_hooks(self):
        """Register built-in hooks."""
        hook_manager.register_path(self.config.hooks)

    def _register_default_tools(self) -> None:
        """Register default set of tools."""
        register_default_tools(
            registry=self.tools,
            config=self.config,
            send_callback=self.bus.publish_outbound,
            subagent_manager=self.subagents,
            cron_service=self.cron_service,
        )

    def _is_knowledge_base_mode(self) -> bool:
        """Whether the agent is restricted to knowledge-base QA mode."""
        return self.config.agents.capability_profile == CapabilityProfile.KNOWLEDGE_BASE

    @staticmethod
    def _has_document_evidence(tools_used: list[dict]) -> bool:
        """Whether tool results contain document-backed evidence for answering."""
        evidence_tools = {"openviking_read", "openviking_grep"}
        for tool_used in tools_used:
            if tool_used.get("tool_name") not in evidence_tools:
                continue
            if not tool_used.get("execute_success"):
                continue
            result = tool_used.get("result")
            if isinstance(result, str) and result.strip():
                return True
        return False

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info("Agent loop started")

        while self._running:
            try:
                # Wait for next message
                msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)

                # Process it
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.exception(f"Error processing message: {e}")
                    # Send error response
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            session_key=msg.session_key,
                            content=f"Sorry, I encountered an error: {str(e)}",
                            metadata=msg.metadata,
                        )
                    )
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    @staticmethod
    def _extract_query_terms(user_request: str) -> list[str]:
        """Extract stable query terms for section matching."""
        stop_terms = {"如何", "怎么", "怎样", "一下", "一个", "这个", "那个", "请问"}
        normalized = user_request.lower()
        terms: set[str] = set()

        for match in re.finditer(r"[a-z0-9][a-z0-9._-]{1,}", normalized):
            terms.add(match.group(0))

        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
            if len(chunk) <= 4 and chunk not in stop_terms:
                terms.add(chunk)
            for index in range(len(chunk) - 1):
                term = chunk[index : index + 2]
                if term not in stop_terms:
                    terms.add(term)

        return sorted(terms, key=len, reverse=True)

    @staticmethod
    def _split_markdown_sections(content: str) -> list[dict[str, str | int]]:
        """Split markdown into heading-based sections."""
        heading_re = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
        matches = list(heading_re.finditer(content))
        if not matches:
            return []

        sections: list[dict[str, str | int]] = []
        for index, match in enumerate(matches):
            level = len(match.group(1))
            end = len(content)
            for next_match in matches[index + 1 :]:
                if len(next_match.group(1)) <= level:
                    end = next_match.start()
                    break

            section_text = content[match.start() : end].strip()
            if not section_text:
                continue

            sections.append(
                {
                    "title": match.group(2).strip(),
                    "level": level,
                    "content": section_text,
                }
            )

        return sections

    @classmethod
    def _score_markdown_section(cls, section: dict[str, str | int], query_terms: list[str]) -> int:
        """Score a markdown section against the user request."""
        title = str(section["title"]).lower()
        content = str(section["content"]).lower()

        title_hits = sum(1 for term in query_terms if term in title)
        content_hits = sum(1 for term in query_terms if term in content)
        image_bonus = 2 if "send://" in content else 0

        return title_hits * 6 + content_hits * 2 + image_bonus

    @classmethod
    def _select_relevant_markdown_section(cls, user_request: str, content: str) -> str | None:
        """Select the most relevant heading-based section from a markdown document."""
        query_terms = cls._extract_query_terms(user_request)
        if not query_terms:
            return None

        sections = cls._split_markdown_sections(content)
        if not sections:
            return None

        best_section: dict[str, str | int] | None = None
        best_score = 0
        for section in sections:
            score = cls._score_markdown_section(section, query_terms)
            if score > best_score:
                best_section = section
                best_score = score

        if not best_section or best_score <= 0:
            return None

        selected = str(best_section["content"]).strip()
        if "send://" not in selected:
            return None
        return selected

    @classmethod
    def _select_relevant_openviking_section(
        cls, user_request: str | None, tools_used: list[dict]
    ) -> str | None:
        """Select one unambiguous image-backed OpenViking section for final formatting."""
        if not user_request:
            return None

        query_terms = cls._extract_query_terms(user_request)
        if not query_terms:
            return None

        section_scores: dict[str, int] = {}
        for tool_used in tools_used:
            if tool_used.get("tool_name") != "openviking_read":
                continue

            result = tool_used.get("result")
            if not isinstance(result, str) or "send://" not in result:
                continue

            selected = cls._select_relevant_markdown_section(user_request, result)
            if not selected:
                continue

            score = sum(1 for term in query_terms if term in selected.lower())
            section_scores[selected] = max(section_scores.get(selected, 0), score)

        if len(section_scores) != 1:
            return None

        selected, score = next(iter(section_scores.items()))
        if score <= 0:
            return None
        return selected

    @staticmethod
    def _normalize_section_title(section: str) -> str:
        """Normalize a markdown heading into a user-facing title."""
        title = re.sub(r"^#{1,6}\s+", "", section.strip(), flags=re.MULTILINE)
        title = title.splitlines()[0].strip() if title else ""
        title = re.sub(r"^[一二三四五六七八九十0-9]+[、.\s]+", "", title)
        return title.strip()

    @staticmethod
    def _prepare_text_block_for_rewrite(text_block: str) -> str:
        """Normalize parser artifacts before rendering grounded evidence."""
        normalized = text_block.replace("\xa0", " ")
        normalized = re.sub(r"\*{2,}", "", normalized)
        normalized = re.sub(r"[ \t]{2,}", " ", normalized)
        normalized = re.sub(r"(?m)^\s*(\d+)[）)]\s*", r"\1. ", normalized)
        normalized = re.sub(r"\n{3,}", "\n\n", normalized)
        return normalized.strip()

    @staticmethod
    def _is_send_image_line(line: str) -> bool:
        """Return whether a line is a standalone sendable image markdown line."""
        return bool(re.match(r"^!\[[^\]]*\]\((send://[^)\s]+)\)\s*$", line.strip()))

    @classmethod
    def _is_step_start(cls, line: str) -> bool:
        """Return whether a normalized line starts a numbered step."""
        normalized = cls._prepare_text_block_for_rewrite(line)
        return bool(re.match(r"^\d+\.\s+\S", normalized))

    @classmethod
    def _append_segment_line(cls, segments: list[dict[str, str]], line: str) -> None:
        """Append a line into the current text segment, creating one when needed."""
        if segments and segments[-1]["type"] == "text":
            segments[-1]["content"] += f"\n{line}"
            return
        segments.append({"type": "text", "content": line})

    @classmethod
    def _append_image_segment(cls, segments: list[dict[str, str]], image_line: str) -> None:
        """Append an image segment while preserving its original relative order."""
        segments.append({"type": "image", "content": image_line.strip()})

    @classmethod
    def _extract_structured_section(
        cls, section: str
    ) -> tuple[str, list[dict[str, str]], list[dict[str, object]]]:
        """Split a section into intro segments and numbered step units."""
        lines = [line.rstrip() for line in section.strip().splitlines()]
        if not lines:
            return "", [], []

        title_line = lines[0].strip()
        body_lines = lines[1:] if title_line.startswith("#") else lines
        intro_segments: list[dict[str, str]] = []
        steps: list[dict[str, object]] = []
        current_step: dict[str, object] | None = None

        def flush_step() -> None:
            nonlocal current_step
            if current_step and current_step["segments"]:
                steps.append(current_step)
            current_step = None

        for line in body_lines:
            stripped = line.strip()
            if not stripped:
                target_segments = intro_segments if current_step is None else current_step["segments"]
                if target_segments and target_segments[-1]["type"] == "text":
                    target_segments[-1]["content"] += "\n"
                continue

            if cls._is_send_image_line(stripped):
                target_segments = intro_segments if current_step is None else current_step["segments"]
                cls._append_image_segment(target_segments, stripped)
                continue

            if cls._is_step_start(stripped):
                flush_step()
                normalized = cls._prepare_text_block_for_rewrite(stripped)
                step_number_match = re.match(r"^(\d+)\.\s+", normalized)
                step_number = step_number_match.group(1) if step_number_match else None
                current_step = {
                    "number": step_number,
                    "segments": [{"type": "text", "content": stripped}],
                }
                continue

            if current_step is None:
                cls._append_segment_line(intro_segments, stripped)
            else:
                cls._append_segment_line(current_step["segments"], stripped)
        flush_step()

        return title_line if title_line.startswith("#") else "", intro_segments, steps

    @classmethod
    def _render_segments(cls, segments: list[dict[str, str]]) -> str:
        """Render structured text/image segments deterministically."""
        parts: list[str] = []
        for segment in segments:
            if segment["type"] == "image":
                parts.append(segment["content"].strip())
                continue

            normalized = cls._prepare_text_block_for_rewrite(segment["content"])
            if normalized:
                parts.append(normalized)

        return "\n\n".join(parts).strip()

    @staticmethod
    def _extract_grounded_intro(draft_reply: str | None) -> str | None:
        """Keep only a short natural lead-in from the agent draft reply."""
        if isinstance(draft_reply, str) and draft_reply.strip():
            for paragraph in re.split(r"\n\s*\n", draft_reply.strip()):
                candidate = paragraph.strip()
                if not candidate:
                    continue
                if candidate.startswith("#") or candidate.startswith("![") or "send://" in candidate:
                    continue
                if re.match(r"^(\d+\.\s+|[-*]\s+)", candidate):
                    continue
                if AgentLoop._contains_internal_reply_language(candidate):
                    continue
                return candidate
        return None

    @staticmethod
    def _contains_internal_reply_language(text: str) -> bool:
        """Return whether a paragraph exposes internal systems or retrieval workflow."""
        normalized = text.lower()
        internal_markers = [
            "openviking",
            "openviking_read",
            "openviking_search",
            "user_memory_search",
            "retrieval",
            "tool result",
            "tool call",
            "上下文数据库",
            "内部工具",
            "工具调用",
            "工具结果",
            "检索结果",
            "生成交付物",
        ]
        return any(marker in normalized for marker in internal_markers)

    async def _build_rewritten_openviking_reply(
        self,
        user_request: str | None,
        tools_used: list[dict],
        draft_reply: str | None = None,
    ) -> tuple[str | None, dict[str, int]]:
        """Build a readable grounded reply without rewriting evidence structure."""
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        if not user_request:
            return None, usage

        best_section = self._select_relevant_openviking_section(user_request, tools_used)
        if not best_section:
            return None, usage

        title_line, intro_segments, steps = self._extract_structured_section(best_section)
        section_title = self._normalize_section_title(title_line) if title_line else ""
        rendered_intro = self._render_segments(intro_segments)
        rendered_steps = [
            self._render_segments(step["segments"])
            for step in steps
            if isinstance(step.get("segments"), list)
        ]
        rendered_steps = [step for step in rendered_steps if step]

        parts: list[str] = []
        intro = self._extract_grounded_intro(draft_reply)
        if intro:
            parts.append(intro)
        elif section_title:
            parts.append(f"## {section_title}")

        if section_title and intro:
            parts.append(f"## {section_title}")

        if rendered_intro:
            parts.append(rendered_intro)

        if rendered_steps:
            parts.extend(rendered_steps)
        elif rendered_intro:
            pass
        else:
            rendered_body = self._prepare_text_block_for_rewrite(best_section)
            if rendered_body:
                parts.append(rendered_body)

        return "\n\n".join(part for part in parts if part).strip(), usage

    async def _run_agent_loop(
        self,
        messages: list[dict],
        session_key: SessionKey,
        publish_events: bool = True,
        sender_id: str | None = None,
        user_request: str | None = None,
        require_document_evidence: bool = False,
    ) -> tuple[str | None, list[dict], dict[str, int]]:
        """
        Run the core agent loop: call LLM, execute tools, repeat until done.

        Args:
            messages: Initial message list
            session_key: Session key for tool execution context
            publish_events: Whether to publish ITERATION/REASONING/TOOL_CALL events to the bus

        Returns:
            tuple of (final_content, tools_used)
        """
        iteration = 0
        final_content = None
        tools_used: list[dict] = []
        token_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        while iteration < self.max_iterations:
            iteration += 1

            if publish_events:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        session_key=session_key,
                        content=f"Iteration {iteration}/{self.max_iterations}",
                        event_type=OutboundEventType.ITERATION,
                    )
                )

            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model,
                session_id=session_key.safe_name(),
            )
            if response.usage:
                cur_token = response.usage
                token_usage["prompt_tokens"] += cur_token["prompt_tokens"]
                token_usage["completion_tokens"] += cur_token["completion_tokens"]
                token_usage["total_tokens"] += cur_token["total_tokens"]

            if publish_events and response.reasoning_content:
                await self.bus.publish_outbound(
                    OutboundMessage(
                        session_key=session_key,
                        content=response.reasoning_content,
                        event_type=OutboundEventType.REASONING,
                    )
                )

            if response.has_tool_calls:
                args_list = [tc.arguments for tc in response.tool_calls]
                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(args),
                        },
                    }
                    for tc, args in zip(response.tool_calls, args_list)
                ]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                # Stage 2: Execute all tools in parallel
                async def execute_single_tool(idx: int, tool_call):
                    """Execute a single tool and track execution time."""
                    tool_execute_start_time = time.time()
                    result = await self.tools.execute(
                        tool_call.name,
                        tool_call.arguments,
                        session_key=session_key,
                        sandbox_manager=self.sandbox_manager,
                        sender_id=sender_id,
                    )
                    tool_execute_duration = (time.time() - tool_execute_start_time) * 1000
                    return idx, tool_call, result, tool_execute_duration

                # Run all tool executions in parallel
                tool_tasks = [
                    execute_single_tool(idx, tool_call)
                    for idx, tool_call in enumerate(response.tool_calls)
                ]
                results = await asyncio.gather(*tool_tasks)

                # Stage 3: Process results sequentially in original order
                for _idx, tool_call, result, tool_execute_duration in results:
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info(f"[TOOL_CALL]: {tool_call.name}({args_str[:200]})")
                    logger.info(f"[RESULT]: {str(result)[:600]}")

                    if publish_events:
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                session_key=session_key,
                                content=f"{tool_call.name}({args_str})",
                                event_type=OutboundEventType.TOOL_CALL,
                            )
                        )
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                session_key=session_key,
                                content=str(result),
                                event_type=OutboundEventType.TOOL_RESULT,
                            )
                        )
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )

                    tool_used_dict = {
                        "tool_name": tool_call.name,
                        "args": args_str,
                        "result": result,
                        "duration": tool_execute_duration,
                        "execute_success": True
                        if result and "Error executing" not in result
                        else False,
                        "input_token": tool_call.tokens,
                        "output_token": cal_str_tokens(result, text_type="mixed"),
                    }
                    tools_used.append(tool_used_dict)

                messages.append(
                    {
                        "role": "system",
                        "content": "Reflect on the results and decide next steps.",
                    }
                )
            else:
                final_content = response.content
                grounded_reply, rewrite_usage = await self._build_rewritten_openviking_reply(
                    user_request=user_request,
                    tools_used=tools_used,
                    draft_reply=final_content,
                )
                if grounded_reply:
                    for key in token_usage:
                        token_usage[key] += rewrite_usage.get(key, 0)
                    final_content = grounded_reply
                break

        if final_content is None:
            if iteration >= self.max_iterations:
                final_content = f"Reached {self.max_iterations} iterations without completion."
            else:
                final_content = "I've completed processing but have no response to give."

        if require_document_evidence and not self._has_document_evidence(tools_used):
            final_content = await generate_route_response(
                provider=self.provider,
                model=self.model,
                route_label="no_evidence",
                user_message=user_request or "",
                session_id=session_key.safe_name(),
            )

        return final_content, tools_used, token_usage

    @trace(
        name="process_message",
        extract_session_id=lambda msg: msg.session_key.safe_name(),
        extract_user_id=lambda msg: msg.sender_id,
    )
    async def _process_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a single inbound message.

        Args:
            msg: The inbound message to process.
            session_key: Override session key (used by process_direct).

        Returns:
            The response message, or None if no response needed.
        """
        # Handle system messages (subagent announces)
        # The chat_id contains the original "channel:chat_id" to route back to
        start_time = time.time()
        long_running_notified = False

        # 监控处理时长，每50秒发送处理中提示事件
        async def check_long_running():
            nonlocal long_running_notified
            tick_count = 0
            # 最多发送7次提示
            max_ticks = 7

            while not long_running_notified and tick_count < max_ticks:
                await asyncio.sleep(40)
                if long_running_notified:
                    break
                if msg.metadata:
                    message_id = msg.metadata.get("message_id")
                    if message_id:
                        try:
                            # 发送处理中tick事件，对应channel会自行处理展示逻辑
                            await self.bus.publish_outbound(
                                OutboundMessage(
                                    session_key=msg.session_key,
                                    content="",
                                    metadata={
                                        "action": "processing_tick",
                                        "tick_count": tick_count,
                                        "message_id": message_id,
                                    },
                                )
                            )
                            tick_count += 1
                        except Exception as e:
                            logger.debug(f"Failed to send processing tick: {e}")

        monitor_task = asyncio.create_task(check_long_running())

        try:
            if msg.session_key.type == "system":
                return await self._process_system_message(msg)

            preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
            logger.info(f"Processing message from {msg.session_key}:{msg.sender_id}: {preview}")

            session_key = msg.session_key
            # For CLI/direct sessions, skip heartbeat by default
            skip_heartbeat = session_key.type == "cli"
            session = self.sessions.get_or_create(session_key, skip_heartbeat=skip_heartbeat)

            # Handle slash commands
            is_group_chat = msg.metadata.get("chat_type") == "group" if msg.metadata else False
            if is_group_chat:
                cmd = msg.content.replace(f"@{msg.sender_id}", "").strip().lower()
            else:
                cmd = msg.content.strip().lower()
            if cmd == "/new":
                # Clone session for async consolidation, then immediately clear original
                if not self._check_cmd_auth(msg):
                    return OutboundMessage(
                        session_key=msg.session_key, content="🐈 Sorry, you are not authorized to use this command.",
                        metadata=msg.metadata
                    )
                session_clone = session.clone()
                session.clear()
                await self.sessions.save(session)
                # Run consolidation in background
                await self._safe_consolidate_memory(session_clone, archive_all=True)
                return OutboundMessage(
                    session_key=msg.session_key, content="🐈 New session started. Memory consolidated.", metadata=msg.metadata
                )
            if cmd == "/remember":
                if not self._check_cmd_auth(msg):
                    return OutboundMessage(
                        session_key=msg.session_key, content="🐈 Sorry, you are not authorized to use this command.",
                        metadata=msg.metadata
                    )
                session_clone = session.clone()
                await self._consolidate_viking_memory(session_clone)
                return OutboundMessage(
                    session_key=msg.session_key, content="This conversation has been submitted to memory storage.", metadata=msg.metadata
                )
            if cmd == "/help":
                return OutboundMessage(
                    session_key=msg.session_key,
                    content="🐈 vikingbot commands:\n/new — Start a new conversation\n/remember — Submit current session to memories and start new session\n/help — Show available commands",
                    metadata=msg.metadata
                )

            # Debug mode handling
            if self.config.mode == BotMode.DEBUG:
                # In debug mode, only record message to session, no processing or reply
                session.add_message("user", msg.content, sender_id=msg.sender_id)
                await self.sessions.save(session)
                return None

            if self._is_knowledge_base_mode():
                intent_decision = await classify_knowledge_base_intent(
                    provider=self.provider,
                    model=self.model,
                    user_message=msg.content,
                    session_id=msg.session_key.safe_name(),
                )
                if intent_decision.route == IntentRoute.META_RESPONSE:
                    meta_reply = await generate_route_response(
                        provider=self.provider,
                        model=self.model,
                        route_label=intent_decision.label,
                        user_message=msg.content,
                        session_id=msg.session_key.safe_name(),
                    )
                    session.add_message(
                        "user",
                        msg.content,
                        sender_id=msg.sender_id,
                    )
                    session.add_message(
                        "assistant",
                        meta_reply,
                        routing_label=intent_decision.label,
                        routing_reason=intent_decision.reason,
                    )
                    await self.sessions.save(session)
                    time_cost = round(time.time() - start_time, 2)
                    return OutboundMessage(
                        session_key=msg.session_key,
                        content=meta_reply,
                        metadata=msg.metadata,
                        time_cost=time_cost,
                    )
                if intent_decision.route == IntentRoute.SAFE_REDIRECT:
                    redirect_reply = await generate_route_response(
                        provider=self.provider,
                        model=self.model,
                        route_label=intent_decision.label,
                        user_message=msg.content,
                        session_id=msg.session_key.safe_name(),
                    )
                    session.add_message(
                        "user",
                        msg.content,
                        sender_id=msg.sender_id,
                        skip_history=True,
                        routing_label=intent_decision.label,
                        routing_reason=intent_decision.reason,
                    )
                    session.add_message(
                        "assistant",
                        redirect_reply,
                        skip_history=True,
                        routing_label=intent_decision.label,
                        routing_reason=intent_decision.reason,
                    )
                    await self.sessions.save(session)
                    logger.info(
                        "Knowledge-base router redirected request: "
                        f"{intent_decision.label} ({intent_decision.reason})"
                    )
                    time_cost = round(time.time() - start_time, 2)
                    return OutboundMessage(
                        session_key=msg.session_key,
                        content=redirect_reply,
                        metadata=msg.metadata,
                        time_cost=time_cost,
                    )

            # Consolidate memory before processing if session is too large
            if len(session.messages) > self.memory_window:
                # Clone session for async consolidation, then immediately trim original
                session_clone = session.clone()
                keep_count = min(10, max(2, self.memory_window // 2))
                session.messages = session.messages[-keep_count:] if keep_count else []
                await self.sessions.save(session)
                # Run consolidation in background
                await self._safe_consolidate_memory(session_clone, archive_all=False)

            if self.sandbox_manager:
                message_workspace = self.sandbox_manager.get_workspace_path(session_key)
            else:
                message_workspace = self.workspace

            from vikingbot.agent.context import ContextBuilder

            message_context = ContextBuilder(
                message_workspace,
                sandbox_manager=self.sandbox_manager,
                sender_id=msg.sender_id,
                is_group_chat=is_group_chat,
                eval=self._eval,
                config=self.config,
            )

            # Build initial messages (use get_history for LLM-formatted messages)
            messages = await message_context.build_messages(
                history=session.get_history(),
                current_message=msg.content,
                media=msg.media if msg.media else None,
                session_key=msg.session_key,
            )
            # logger.info(f"New messages: {messages}")

            # Run agent loop
            final_content, tools_used, token_usage = await self._run_agent_loop(
                messages=messages,
                session_key=session_key,
                publish_events=True,
                sender_id=msg.sender_id,
                user_request=msg.content,
                require_document_evidence=self._is_knowledge_base_mode(),
            )

            # Log response preview
            preview = final_content[:300] + "..." if len(final_content) > 300 else final_content
            logger.info(f"Response to {msg.session_key}: {preview}")

            # Save to session (include tool names so consolidation sees what happened)
            session.add_message("user", msg.content, sender_id=msg.sender_id)
            session.add_message(
                "assistant", final_content, tools_used=tools_used if tools_used else None, token_usage=token_usage,
                sender_id=msg.sender_id,
            )
            await self.sessions.save(session)

            time_cost = round(time.time() - start_time, 2)
            return OutboundMessage(
                session_key=msg.session_key,
                content=final_content,
                metadata=msg.metadata,
                token_usage=token_usage,
                time_cost=time_cost
                or {},  # Pass through for channel-specific needs (e.g. Slack thread_ts)
            )
        finally:
            long_running_notified = True
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass

    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage | None:
        """
        Process a system message (e.g., subagent announce).

        The chat_id field contains "original_channel:original_chat_id" to route
        the response back to the correct destination.
        """
        logger.info(f"Processing system message from {msg.sender_id}")

        session = self.sessions.get_or_create(msg.session_key)

        # Build messages with the announce content
        messages = await self.context.build_messages(
            history=session.get_history(), current_message=msg.content, session_key=msg.session_key
        )

        # Run agent loop (no events published)
        final_content, tools_used, token_usage = await self._run_agent_loop(
            messages=messages,
            session_key=msg.session_key,
            publish_events=False,
            user_request=msg.content,
            require_document_evidence=self._is_knowledge_base_mode(),
        )

        if final_content is None:
            final_content = "Background task completed."

        # Save to session (mark as system message in history)
        session.add_message("user", f"[System: {msg.sender_id}] {msg.content}")
        session.add_message(
            "assistant", final_content, tools_used=tools_used if tools_used else None
        )
        await self.sessions.save(session)

        return OutboundMessage(session_key=msg.session_key, content=final_content)

    async def _consolidate_memory(self, session, archive_all: bool = False) -> None:
        """Consolidate old messages into MEMORY.md + HISTORY.md. Works on a cloned session."""
        try:
            if not session.messages:
                return

            # use openviking tools to extract memory
            config = self.config
            if config.mode == BotMode.READONLY:
                if not config.channels_config or not config.channels_config.get_all_channels():
                    return
                allow_from = [config.ov_server.admin_user_id]
                for channel_config in config.channels_config.get_all_channels():
                    if channel_config and channel_config.type.value == session.key.type:
                        if hasattr(channel_config, "allow_from"):
                            allow_from.extend(channel_config.allow_from)
                messages = [msg for msg in session.messages if msg.get("sender_id") in allow_from]
                session.messages = messages
            await self._consolidate_viking_memory(session)

            if self.sandbox_manager:
                memory_workspace = self.sandbox_manager.get_workspace_path(session.key)
            else:
                memory_workspace = self.workspace

            memory = MemoryStore(memory_workspace)
            if archive_all:
                old_messages = session.messages
                keep_count = 0
            else:
                keep_count = min(10, max(2, self.memory_window // 2))
                old_messages = session.messages[:-keep_count]
            if not old_messages:
                return
            logger.info(
                f"Memory consolidation started: {len(session.messages)} messages, archiving {len(old_messages)}, keeping {keep_count}"
            )

            # Format messages for LLM (include tool names when available)
            lines = []
            for m in old_messages:
                if not m.get("content"):
                    continue
                tools_used = m.get("tools_used", [])
                if tools_used and isinstance(tools_used, list):
                    tool_names = [
                        tc.get("tool_name", "unknown") for tc in tools_used if isinstance(tc, dict)
                    ]
                    tools_str = f" [tools: {', '.join(tool_names)}]" if tool_names else ""
                else:
                    tools_str = ""
                lines.append(
                    f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools_str}: {m['content']}"
                )
            conversation = "\n".join(lines)
            current_memory = memory.read_long_term()

            prompt = f"""You are a memory consolidation agent. Process this conversation and return a JSON object with exactly two keys:

1. "history_entry": A paragraph (2-5 sentences) summarizing the key events/decisions/topics. Start with a timestamp like [YYYY-MM-DD HH:MM]. Include enough detail to be useful when found by grep search later.

2. "memory_update": The updated long-term memory content. Add any new facts: user location, preferences, personal info, habits, project context, technical decisions, tools/services used. If nothing new, return the existing content unchanged.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{conversation}

Respond with ONLY valid JSON, no markdown fences."""

            response = await self.provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": "You are a memory consolidation agent. Respond only with valid JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self.model,
                session_id=session.key.safe_name(),
            )
            text = (response.content or "").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
            result = json.loads(text)

            if entry := result.get("history_entry"):
                memory.append_history(entry)
            if update := result.get("memory_update"):
                if load_config().use_local_memory and update != current_memory:
                    memory.write_long_term(update)

            # Session trimming and saving is handled by the caller before calling _consolidate_memory
            # This method works on a cloned session, so no need to save it
            logger.info("Memory consolidation done")
        except Exception as e:
            logger.exception(f"Memory consolidation failed: {e}")

    async def _consolidate_viking_memory(self, session) -> None:
        """Consolidate old messages into MEMORY.md + HISTORY.md. Works on a cloned session."""
        try:
            if not session.messages:
                logger.info(f"No messages to commit openviking for session {session.key.safe_name()} (allow_from filter applied)")
                return

            # use openviking tools to extract memory
            await hook_manager.execute_hooks(
                context=HookContext(
                    event_type="message.compact",
                    session_id=session.key.safe_name(),
                    workspace_id=self.sandbox_manager.to_workspace_id(session.key),
                    session_key=session.key,
                ),
                session=session,
            )
        except Exception as e:
            logger.exception(f"Memory consolidation failed: {e}")

    async def _safe_consolidate_memory(self, session, archive_all: bool = False) -> None:
        """Safe wrapper for _consolidate_memory that ensures all exceptions are caught."""
        try:
            await self._consolidate_memory(session, archive_all)
        except Exception as e:
            logger.exception(f"Background memory consolidation task failed: {e}")

    def _check_cmd_auth(self, msg: InboundMessage) -> bool:
        """Check if the session key is authorized for command execution.

        Returns:
            True if authorized, False otherwise.
        Args:
            session_key: Session key to check.
        """
        if self.config.mode == BotMode.NORMAL:
            return True
        allow_from = []
        if self.config.ov_server and self.config.ov_server.admin_user_id:
            allow_from.append(self.config.ov_server.admin_user_id)
        for channel in self.config.channels_config.get_all_channels():
            if channel.channel_key() == msg.session_key.channel_key():
                if channel.allow_from:
                    allow_from.extend(channel.allow_from)
                break

        # If channel not found or sender not in allow_from list, ignore message
        if msg.sender_id not in allow_from:
            logger.debug(f"Sender {msg.sender_id} not allowed in channel {msg.session_key.channel_key()}")
            return False
        return True

    async def process_direct(
        self,
        content: str,
        session_key: SessionKey = SessionKey(type="cli", channel_id="default", chat_id="direct"),
    ) -> str:
        """
        Process a message directly (for CLI or cron usage).

        Args:
            content: The message content.
            session_key: Session identifier (overrides channel:chat_id for session lookup).

        Returns:
            The agent's response.
        """
        msg = InboundMessage(session_key=session_key, sender_id="user", content=content)

        response = await self._process_message(msg)
        return response.content if response else ""
