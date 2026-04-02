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
from vikingbot.config.schema import BotMode, Config, SessionKey
from vikingbot.hooks import HookContext
from vikingbot.hooks.manager import hook_manager
from vikingbot.openviking_mount.uri_utils import is_generic_scope_summary_uri, is_summary_uri
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
        self.fast_model = (
            config.agents.fast_model
            if config and hasattr(config.agents, "fast_model")
            else "dashscope/qwen-turbo"
        )
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

    async def _run_agent_loop(
        self,
        messages: list[dict],
        session_key: SessionKey,
        publish_events: bool = True,
        sender_id: str | None = None,
    ) -> tuple[str | None, list[dict], dict[str, int], int]:
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
        has_kb_read_evidence = False

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

            tool_definitions = self.tools.get_definitions()
            tool_choice = self._select_tool_choice(
                iteration=iteration,
                tools=tool_definitions,
                has_kb_read_evidence=has_kb_read_evidence,
            )
            response = await self.provider.chat(
                messages=messages,
                tools=tool_definitions,
                tool_choice=tool_choice,
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
            elif publish_events and response.has_tool_calls:
                plan_summary = self._build_tool_plan_summary(response.tool_calls)
                if plan_summary:
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            session_key=session_key,
                            content=plan_summary,
                            event_type=OutboundEventType.REASONING,
                        )
                    )
            elif (
                publish_events
                and self.context._is_knowledge_base_mode()
                and not has_kb_read_evidence
                and not response.has_tool_calls
            ):
                plan_summary = self._summarize_non_tool_kb_response(response.content)
                if plan_summary:
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            session_key=session_key,
                            content=plan_summary,
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

                # Publish tool-call events before execution so streaming clients can
                # see which tools are starting rather than waiting for results.
                if publish_events:
                    for tool_call in response.tool_calls:
                        args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                        await self.bus.publish_outbound(
                            OutboundMessage(
                                session_key=session_key,
                                content=f"{tool_call.name}({args_str})",
                                event_type=OutboundEventType.TOOL_CALL,
                            )
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
                    has_kb_read_evidence = has_kb_read_evidence or self._is_concrete_kb_read_result(
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                        result=result,
                    )

                messages.append(
                    {"role": "system", "content": self.context.build_tool_reflection_prompt()}
                )
            else:
                if self.context._is_knowledge_base_mode() and not has_kb_read_evidence:
                    if response.content or response.reasoning_content:
                        messages = self.context.add_assistant_message(
                            messages,
                            response.content,
                            reasoning_content=response.reasoning_content,
                        )
                    messages.append(
                        {
                            "role": "system",
                            "content": self.context.build_kb_continue_search_prompt(
                                progress_summary=self._summarize_kb_tool_state(messages)
                            ),
                        }
                    )
                    continue
                final_content = response.content
                break

        if final_content is None or (
            isinstance(final_content, str) and not final_content.strip()
        ):
            if iteration >= self.max_iterations:
                final_content = f"Reached {self.max_iterations} iterations without completion."
            else:
                final_content = "I've completed processing but have no response to give."

        if final_content:
            final_content = await self._finalize_kb_response(final_content, session_key, messages)

        return final_content, tools_used, token_usage, iteration

    @staticmethod
    def _is_concrete_kb_read_result(tool_name: str, arguments: dict, result: str) -> bool:
        """Whether a tool result represents a concrete KB document read."""
        if tool_name != "openviking_read":
            return False
        if not isinstance(arguments, dict):
            return False
        if arguments.get("level", "abstract") != "read":
            return False

        uri = str(arguments.get("uri", "") or "")
        if not uri or is_summary_uri(uri) or is_generic_scope_summary_uri(uri):
            return False

        if not isinstance(result, str) or not result.strip():
            return False
        if result.startswith("Error reading from Viking:"):
            return False
        if "不能直接执行 level='read'" in result:
            return False
        if "下没有可读取的正文文件" in result:
            return False
        return True

    @classmethod
    def _summarize_kb_tool_state(cls, messages: list[dict]) -> str:
        """Summarize KB retrieval state for the next search iteration."""
        lines: list[str] = []
        for message in messages:
            if message.get("role") != "tool":
                continue
            tool_name = message.get("name") or "unknown_tool"
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                snippet = content.strip().replace("\n", " ")
                lines.append(f"- {tool_name}: {snippet[:200]}")
            else:
                lines.append(f"- {tool_name}")
        return "\n".join(lines[-6:]) if lines else "No KB tool evidence collected yet."

    def _select_tool_choice(
        self,
        iteration: int,
        tools: list[dict] | None,
        has_kb_read_evidence: bool,
    ) -> str | None:
        """Select tool-choice mode for the current LLM turn."""
        if (
            self.context._is_knowledge_base_mode()
            and iteration == 1
            and not has_kb_read_evidence
            and tools
        ):
            return "required"
        return None

    @staticmethod
    def _build_tool_plan_summary(tool_calls: list) -> str | None:
        """Build a short user-facing summary of the model's planned tool steps."""
        if not tool_calls:
            return None

        def _clean_text(value: object, max_len: int = 36) -> str:
            text = str(value or "").strip().replace("\n", " ")
            text = re.sub(r"\s+", " ", text)
            if len(text) > max_len:
                return f"{text[: max_len - 3]}..."
            return text

        def _short_name_from_path(value: object) -> str:
            text = _clean_text(value)
            if not text:
                return ""
            return text.rsplit("/", 1)[-1] or text

        plan_steps: list[str] = []
        for tool_call in tool_calls[:3]:
            args = tool_call.arguments if isinstance(tool_call.arguments, dict) else {}
            name = str(getattr(tool_call, "name", "") or "")

            if name == "openviking_search":
                query = _clean_text(
                    args.get("query") or args.get("keyword") or args.get("q") or "当前问题"
                )
                plan_steps.append(f"先搜索相关资料：{query}")
            elif name == "openviking_read":
                target = _short_name_from_path(args.get("uri"))
                plan_steps.append(f"再读取文档内容{f'：{target}' if target else ''}")
            elif name == "openviking_glob":
                plan_steps.append("先定位具体文档")
            elif "search" in name:
                query = _clean_text(
                    args.get("query") or args.get("keyword") or args.get("q") or "相关信息"
                )
                plan_steps.append(f"先检索信息：{query}")
            elif "read" in name:
                target = _short_name_from_path(
                    args.get("uri") or args.get("path") or args.get("file_path")
                )
                plan_steps.append(f"再读取内容{f'：{target}' if target else ''}")
            elif "exec" in name or "shell" in name or "python" in name:
                plan_steps.append("执行必要的检查和计算")
            else:
                plan_steps.append(f"执行 {name}")

        if not plan_steps:
            return None

        summary = "规划：" + " -> ".join(plan_steps)
        if len(tool_calls) > len(plan_steps):
            summary += f" 等 {len(tool_calls)} 个步骤"
        return summary

    @staticmethod
    def _summarize_non_tool_kb_response(content: str | None, max_len: int = 240) -> str | None:
        """Surface a KB model draft when it failed to emit a tool call."""
        if not isinstance(content, str):
            return None

        text = re.sub(r"\s+", " ", content).strip()
        if not text:
            return None

        if len(text) > max_len:
            return f"{text[: max_len - 3]}..."
        return text

    @classmethod
    def _extract_image_evidence_blocks(cls, messages: list[dict]) -> list[str]:
        """Collect tool evidence blocks that already preserve text-image association."""
        seen: set[str] = set()
        blocks: list[str] = []
        for message in messages:
            if message.get("role") != "tool":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            if not cls.SEND_IMAGE_LINE_RE.search(content):
                continue
            normalized = content.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                blocks.append(normalized)
        return blocks

    @classmethod
    def _extract_send_image_lines_from_text(cls, content: str) -> list[str]:
        """Collect unique send:// Markdown image lines from a text block."""
        seen: set[str] = set()
        lines: list[str] = []
        for match in cls.SEND_IMAGE_LINE_RE.finditer(content):
            line = match.group(0)
            if line not in seen:
                seen.add(line)
                lines.append(line)
        return lines

    @staticmethod
    def _parse_selected_segment_indexes(selection_text: str, max_index: int) -> list[int]:
        """Parse segment indexes from a model selection response."""
        return sorted(
            {
                int(match.group(0))
                for match in re.finditer(r"\d+", selection_text or "")
                if 1 <= int(match.group(0)) <= max_index
            }
        )

    @classmethod
    def _build_image_evidence_segments(cls, blocks: list[str]) -> list[str]:
        """Split image evidence blocks into smaller text-image segments."""
        segments: list[str] = []
        for block in blocks:
            text_buffer: list[str] = []
            image_buffer: list[str] = []
            for raw_line in block.splitlines():
                line = raw_line.rstrip()
                is_image_line = bool(cls.SEND_IMAGE_LINE_RE.fullmatch(line.strip()))
                if is_image_line:
                    image_buffer.append(line.strip())
                    continue
                if image_buffer:
                    segment_parts = ["\n".join(part for part in text_buffer if part).strip()]
                    segment_parts.append("\n".join(image_buffer))
                    segment = "\n\n".join(part for part in segment_parts if part).strip()
                    if segment:
                        segments.append(segment)
                    text_buffer = [line] if line else []
                    image_buffer = []
                    continue
                text_buffer.append(line)

            if image_buffer:
                segment_parts = ["\n".join(part for part in text_buffer if part).strip()]
                segment_parts.append("\n".join(image_buffer))
                segment = "\n\n".join(part for part in segment_parts if part).strip()
                if segment:
                    segments.append(segment)

        return segments

    async def _select_relevant_image_segments(
        self, draft_content: str, image_segments: list[str], session_key: SessionKey
    ) -> list[str]:
        """Ask the model to choose the minimum image segments needed for the answer."""
        if not image_segments:
            return []

        numbered_segments = "\n\n".join(
            f"[Segment {index}]\n{segment}" for index, segment in enumerate(image_segments, start=1)
        )
        selection_messages = [
            {
                "role": "system",
                "content": (
                    "Select the minimum image evidence segments needed to support the answer. "
                    "Return only segment numbers separated by commas. "
                    "Do not include any explanation."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Answer draft:\n"
                    f"{draft_content}\n\n"
                    "Candidate image evidence segments:\n"
                    f"{numbered_segments}"
                ),
            },
        ]
        selection = await self.provider.chat(
            messages=selection_messages,
            model=self.fast_model,
            session_id=f"{session_key.safe_name()}:kb-image-select",
        )
        indexes = self._parse_selected_segment_indexes(selection.content or "", len(image_segments))
        if not indexes:
            retry_messages = [
                {
                    "role": "system",
                    "content": (
                        "Return only valid segment numbers separated by commas. "
                        "Do not include any words or explanation."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "You must choose one or more segment numbers from the list below.\n\n"
                        f"{numbered_segments}"
                    ),
                },
            ]
            retry = await self.provider.chat(
                messages=retry_messages,
                model=self.fast_model,
                session_id=f"{session_key.safe_name()}:kb-image-select-retry",
            )
            indexes = self._parse_selected_segment_indexes(retry.content or "", len(image_segments))
        return [image_segments[index - 1] for index in indexes]

    async def _finalize_kb_response(
        self, draft_content: str, session_key: SessionKey, messages: list[dict]
    ) -> str:
        """Rewrite a KB draft into one direct user-facing answer.

        Optimization: if the draft contains no image evidence, skip the final
        LLM rewrite entirely and return the draft as-is.  This removes one
        expensive LLM round-trip from the critical path.
        """
        if not self.context._is_knowledge_base_mode() or not draft_content:
            return draft_content

        image_evidence_blocks = self._extract_image_evidence_blocks(messages)
        image_evidence_segments = self._build_image_evidence_segments(image_evidence_blocks)
        selected_image_segments = await self._select_relevant_image_segments(
            draft_content, image_evidence_segments, session_key
        )
        should_include_images = bool(selected_image_segments)

        # Fast path: no images → return draft directly, no rewrite LLM call needed
        if not should_include_images:
            return draft_content

        preserve_block = (
            "\n\nThe tool evidence below already preserves the association between explanatory "
            "text and screenshots. When composing the final reply, keep the relevant image "
            "Markdown lines exactly as written and keep each image near the text it illustrates. "
            "Do not move all images to the end.\n\n"
            "Image-aware evidence:\n"
            + "\n\n---\n\n".join(selected_image_segments)
        )

        final_messages = [
            {
                "role": "system",
                "content": self.context.build_kb_final_response_system_prompt(),
            },
            {
                "role": "user",
                "content": (
                    "Rewrite the following draft into one direct final reply for the user.\n"
                    "Remove any mention of searching, reading documents, internal progress, or tool usage.\n\n"
                    f"Draft:\n{draft_content}{preserve_block}"
                ),
            },
        ]

        response = await self.provider.chat(
            messages=final_messages,
            model=self.model,
            session_id=f"{session_key.safe_name()}:kb-final",
        )
        final_content = response.content or draft_content

        allowed_image_lines: list[str] = []
        for block in selected_image_segments:
            allowed_image_lines.extend(self._extract_send_image_lines_from_text(block))
        allowed_image_lines = list(dict.fromkeys(allowed_image_lines))

        if allowed_image_lines:
            allowed_set = set(allowed_image_lines)
            output_image_lines = self._extract_send_image_lines_from_text(final_content)
            unexpected_lines = [line for line in output_image_lines if line not in allowed_set]
            if unexpected_lines:
                correction_messages = [
                    {
                        "role": "system",
                        "content": self.context.build_kb_final_response_system_prompt(),
                    },
                    {
                        "role": "user",
                        "content": (
                            "Rewrite the final reply again.\n"
                            "You used Markdown image lines that were not present in the evidence.\n"
                            "You may use only the exact image Markdown lines listed below, and no other send:// references.\n\n"
                            "Allowed image Markdown lines:\n"
                            + "\n".join(allowed_image_lines)
                            + "\n\nReply draft to correct:\n"
                            + final_content
                        ),
                    },
                ]
                correction = await self.provider.chat(
                    messages=correction_messages,
                    model=self.fast_model,
                    session_id=f"{session_key.safe_name()}:kb-final-correct",
                )
                final_content = correction.content or final_content

        return final_content

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

            # Knowledge-base mode: classify intent before agent loop
            if message_context._is_knowledge_base_mode():
                try:
                    logger.info("[IntentRouter] Classifying user intent...")
                    decision = await classify_knowledge_base_intent(
                        provider=self.provider,
                        model=self.fast_model,
                        user_message=msg.content,
                        session_id=session_key.safe_name(),
                    )
                    logger.info(
                        f"[IntentRouter] label={decision.label} route={decision.route} "
                        f"confidence={decision.confidence} reason={decision.reason}"
                    )

                    if decision.route != IntentRoute.AGENT:
                        # Non-retrieval route: generate a direct response
                        response_text = await generate_route_response(
                            provider=self.provider,
                            model=self.model,
                            route_label=decision.label,
                            user_message=msg.content,
                            session_id=session_key.safe_name(),
                        )
                        # Save to session
                        session.add_message("user", msg.content, sender_id=msg.sender_id)
                        session.add_message("assistant", response_text, sender_id=msg.sender_id)
                        await self.sessions.save(session)

                        time_cost = round(time.time() - start_time, 2)
                        return OutboundMessage(
                            session_key=msg.session_key,
                            content=response_text,
                            metadata=msg.metadata,
                            time_cost=time_cost,
                        )
                except Exception as e:
                    logger.warning(
                        f"[IntentRouter] Classification failed, falling back to agent loop: {e}",
                        exc_info=True,
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
            final_content, tools_used, token_usage, iteration = await self._run_agent_loop(
                messages=messages,
                session_key=session_key,
                publish_events=True,
                sender_id=msg.sender_id,
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
            if tools_used is not None:
                tools_used_names = [tool["tool_name"] for tool in tools_used]
            else:
                tools_used_names = []
            return OutboundMessage(
                session_key=msg.session_key,
                content=final_content,
                metadata=msg.metadata,
                token_usage=token_usage,
                time_cost=time_cost,
                iteration=iteration,
                tools_used_names=tools_used_names
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
        final_content, tools_used, token_usage, iteration = await self._run_agent_loop(
            messages=messages,
            session_key=msg.session_key,
            publish_events=False,
        )

        if final_content is None or (
            isinstance(final_content, str) and not final_content.strip()
        ):
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
                allow_cmd = getattr(channel, 'allow_cmd_from', [])
                if allow_cmd:
                    allow_from.extend(allow_cmd)
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
    SEND_IMAGE_LINE_RE = re.compile(r"!\[[^\]]*\]\((send://[^)\s]+)\)")
