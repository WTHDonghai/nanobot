"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote

from loguru import logger

from openviking_cli.resource_preview import (
    RESOURCE_PREVIEW_SECRET_ENV,
    ResourcePreviewTokenError,
    create_resource_preview_token,
)
from vikingbot.agent.context import ContextBuilder
from vikingbot.agent.intent_router import (
    IntentRoute,
    classify_intent,
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
from vikingbot.hooks.builtins.openviking_hooks import mirror_messages_to_openviking
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


@dataclass
class _SemanticEvidenceSelection:
    """Relevant sections plus a semantic coverage decision for the request."""

    sections: list[str]
    coverage: str = "unknown"
    missing: str = ""
    next_query: str = ""


@dataclass
class _FastBatchSearchPlan:
    """Deterministic KB retrieval plan derived from one focused search result."""

    document_uris: list[str]
    total_matches: int | None = None


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

    SEND_IMAGE_LINE_RE = re.compile(r"!\[[^\]]*\]\((send://[^)\s]+)\)")
    MARKDOWN_IMAGE_LINE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
    MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+.+\s*$")
    NUMBERED_HEADING_RE = re.compile(
        r"^\s*(?:\*\*)?\s*(?:第[一二三四五六七八九十百千]+[章节节、]|"
        r"[一二三四五六七八九十]+[、.．]|"
        r"\d+(?:\.\d+){0,4})\s*[\u4e00-\u9fffA-Za-z][^\n]{0,80}?(?:\*\*)?\s*$"
    )
    GROUNDED_HISTORY_ANSWER_TOOL = "answer_from_grounded_history"
    KB_FAST_BATCH_SEARCH_LIMIT = 8
    KB_FAST_BATCH_READ_LIMIT = 4
    KB_FAST_BATCH_MAX_EVIDENCE_BLOCKS = 8

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
        self._max_concurrent_inbound = self._resolve_max_concurrent_inbound()
        self._inbound_semaphore = asyncio.Semaphore(self._max_concurrent_inbound)
        self._inflight_message_tasks: set[asyncio.Task[None]] = set()
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._openviking_sync_tasks: dict[str, asyncio.Task[None]] = {}
        self._register_default_tools()

    def _should_use_kb_fast_batch_path(self) -> bool:
        """Use deterministic batch retrieval for KB answers when search/read tools exist."""
        if not self.context._is_retrieval_mode():
            return False

        return self._has_kb_fast_batch_tools()

    def _has_kb_fast_batch_tools(self) -> bool:
        """Whether the configured tool registry can run the KB fast batch path."""
        tool_names: set[str] = set()
        for definition in self.tools.get_definitions() or []:
            if not isinstance(definition, dict):
                continue
            function = definition.get("function")
            if not isinstance(function, dict):
                continue
            name = str(function.get("name") or "").strip()
            if name:
                tool_names.add(name)

        has_fast_tools = {"openviking_search", "openviking_read"}.issubset(tool_names)
        if not has_fast_tools:
            logger.info(
                "[KB_TRACE] retrieval_path=fast_batch_unavailable "
                "reason=missing_required_tools "
                f"available_tools={sorted(tool_names)}"
            )
        return has_fast_tools

    @classmethod
    def _build_fast_batch_search_plan(
        cls,
        *,
        search_result: str,
        max_documents: int,
    ) -> _FastBatchSearchPlan:
        """Extract concrete document URIs from formatted openviking_search output."""
        document_uris: list[str] = []
        seen: set[str] = set()

        for match in re.finditer(
            r"(?m)^\s*\d+\.\s+\[document\]\s+(viking://\S+)\s*$",
            str(search_result or ""),
        ):
            uri = match.group(1).strip().rstrip(".,;:")
            if (
                not uri
                or uri in seen
                or is_summary_uri(uri)
                or is_generic_scope_summary_uri(uri)
            ):
                continue
            seen.add(uri)
            document_uris.append(uri)
            if len(document_uris) >= max_documents:
                break

        total_matches: int | None = None
        total_match = re.search(r"(?m)^Total matches:\s*(\d+)\s*$", str(search_result or ""))
        if total_match:
            try:
                total_matches = int(total_match.group(1))
            except ValueError:
                total_matches = None

        return _FastBatchSearchPlan(
            document_uris=document_uris,
            total_matches=total_matches,
        )

    @staticmethod
    def _tool_call_dict(tool_call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        }

    async def _publish_tool_call_event(
        self,
        *,
        session_key: SessionKey,
        tool_name: str,
        arguments: dict[str, Any],
        publish_events: bool,
    ) -> None:
        if not publish_events:
            return
        args_str = json.dumps(arguments, ensure_ascii=False)
        await self.bus.publish_outbound(
            OutboundMessage(
                session_key=session_key,
                content=f"{tool_name}({args_str})",
                event_type=OutboundEventType.TOOL_CALL,
            )
        )

    async def _publish_tool_result_event(
        self,
        *,
        session_key: SessionKey,
        result: str,
        publish_events: bool,
    ) -> None:
        if not publish_events:
            return
        await self.bus.publish_outbound(
            OutboundMessage(
                session_key=session_key,
                content=str(result),
                event_type=OutboundEventType.TOOL_RESULT,
            )
        )

    async def _execute_fast_batch_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        session_key: SessionKey,
        sender_id: str | None,
    ) -> tuple[str, float]:
        start_time = time.time()
        result = await self.tools.execute(
            tool_name,
            arguments,
            session_key=session_key,
            sandbox_manager=self.sandbox_manager,
            sender_id=sender_id,
        )
        return str(result or ""), (time.time() - start_time) * 1000

    @staticmethod
    def _tool_record(
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
        duration_ms: float,
    ) -> dict[str, Any]:
        return {
            "tool_name": tool_name,
            "args": json.dumps(arguments, ensure_ascii=False),
            "result": result,
            "duration": duration_ms,
            "execute_success": bool(result and "Error executing" not in result),
            "input_token": 0,
            "output_token": cal_str_tokens(result, text_type="mixed"),
        }

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

    def _resolve_max_concurrent_inbound(self) -> int:
        """Resolve the maximum number of inbound messages to process concurrently."""
        if not self.config:
            return 1

        try:
            channel_configs = self.config.channels_config.get_all_channels()
        except Exception:
            channel_configs = self.config.channels or []

        limits: list[int] = []
        for channel in channel_configs:
            if not getattr(channel, "enabled", True):
                continue
            limit = getattr(channel, "max_concurrent_requests", None)
            if isinstance(limit, int) and limit > 0:
                limits.append(limit)

        return max(limits, default=1)

    def _register_default_tools(self) -> None:
        """Register default set of tools."""
        register_default_tools(
            registry=self.tools,
            config=self.config,
            send_callback=self.bus.publish_outbound,
            subagent_manager=self.subagents,
            cron_service=self.cron_service,
            knowledge_base_mode=self.context._is_knowledge_base_mode(),
        )

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        logger.info(
            "Agent loop started (max_concurrent_inbound={})",
            self._max_concurrent_inbound,
        )

        try:
            while self._running:
                try:
                    await asyncio.wait_for(self._inbound_semaphore.acquire(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                try:
                    msg = await asyncio.wait_for(self.bus.consume_inbound(), timeout=1.0)
                except asyncio.TimeoutError:
                    self._inbound_semaphore.release()
                    continue

                task = asyncio.create_task(self._process_inbound_message(msg))
                self._inflight_message_tasks.add(task)
                task.add_done_callback(self._on_inflight_message_done)
        finally:
            for task in list(self._inflight_message_tasks):
                task.cancel()
            if self._inflight_message_tasks:
                await asyncio.gather(*self._inflight_message_tasks, return_exceptions=True)
            sync_tasks = list(self._openviking_sync_tasks.values())
            for task in sync_tasks:
                task.cancel()
            if sync_tasks:
                await asyncio.gather(*sync_tasks, return_exceptions=True)

    def stop(self) -> None:
        """Stop the agent loop."""
        self._running = False
        logger.info("Agent loop stopping")

    def _on_inflight_message_done(self, task: asyncio.Task[None]) -> None:
        """Remove completed inbound tasks from the tracking set."""
        self._inflight_message_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"Unhandled error in inbound task: {e}")

    async def _process_inbound_message(self, msg: InboundMessage) -> None:
        """Process one inbound message with per-session ordering guarantees."""
        session_lock = self._session_locks.setdefault(msg.session_key.safe_name(), asyncio.Lock())

        try:
            async with session_lock:
                try:
                    response = await self._process_message(msg)
                    if response:
                        await self.bus.publish_outbound(response)
                except Exception as e:
                    logger.exception(f"Error processing message: {e}")
                    await self.bus.publish_outbound(
                        OutboundMessage(
                            session_key=msg.session_key,
                            content=f"Sorry, I encountered an error: {str(e)}",
                            metadata=msg.metadata,
                        )
                    )
        finally:
            self._inbound_semaphore.release()

    @staticmethod
    def _message_has_openviking_payload(message: dict[str, Any]) -> bool:
        """Return True when a session message contains syncable OpenViking content."""
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return True
        tools_used = message.get("tools_used")
        return isinstance(tools_used, list) and len(tools_used) > 0

    @staticmethod
    def _metadata_indicates_shared_session(metadata: dict[str, Any] | None) -> bool:
        """Detect channel metadata that implies multiple human participants share one session."""
        if not metadata:
            return False
        if metadata.get("chat_type") == "group":
            return True
        if metadata.get("is_group") is True:
            return True
        for key in ("group_id", "groupId"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return True
        return False

    def _resolve_openviking_agent_owner_user_id(self) -> str:
        """Return the configured stable owner used for shared agent-memory extraction."""
        user_id = getattr(getattr(self.config, "ov_server", None), "admin_user_id", "")
        if isinstance(user_id, str) and user_id.strip():
            return user_id.strip()
        raise ValueError("Missing ov_server.admin_user_id for agent-scoped OpenViking sync")

    def _resolve_session_memory_scope(self, session, msg: InboundMessage) -> str:
        """Resolve whether this bot session should extract all memories or agent-only memories."""
        metadata = session.metadata if isinstance(session.metadata, dict) else {}
        existing_scope = metadata.get("openviking_memory_scope")
        if isinstance(existing_scope, str):
            normalized_scope = existing_scope.strip().lower()
            if normalized_scope == "agent":
                return "agent"
            if normalized_scope in {"all", "user"}:
                existing_scope = normalized_scope
            else:
                raise ValueError(
                    f"Invalid openviking_memory_scope stored on session: {existing_scope}"
                )

        if self._metadata_indicates_shared_session(msg.metadata):
            return "agent"

        historical_senders = {
            str(sender_id).strip()
            for sender_id in (message.get("sender_id") for message in session.messages)
            if isinstance(sender_id, str) and sender_id.strip()
        }
        if len(historical_senders) > 1:
            return "agent"

        current_sender_id = msg.sender_id.strip() if isinstance(msg.sender_id, str) else ""
        if historical_senders and current_sender_id and current_sender_id not in historical_senders:
            return "agent"

        if isinstance(existing_scope, str):
            return existing_scope
        return "all"

    def _apply_openviking_memory_policy(self, session, msg: InboundMessage) -> None:
        """Persist the explicit OpenViking extraction policy on the local bot session."""
        memory_scope = self._resolve_session_memory_scope(session, msg)
        session.metadata["openviking_memory_scope"] = memory_scope

        if memory_scope == "agent":
            session.metadata["openviking_agent_owner_user_id"] = (
                self._resolve_openviking_agent_owner_user_id()
            )
            session.metadata.pop("openviking_user_id", None)
            return

        sender_id = msg.sender_id.strip() if isinstance(msg.sender_id, str) else ""
        if not sender_id:
            raise ValueError("Missing sender_id for user-scoped OpenViking session policy")
        session.metadata["openviking_user_id"] = sender_id
        session.metadata.pop("openviking_agent_owner_user_id", None)

    def _on_openviking_sync_done(self, session_name: str, task: asyncio.Task[None]) -> None:
        """Clean up completed background OpenViking sync tasks."""
        if self._openviking_sync_tasks.get(session_name) is task:
            self._openviking_sync_tasks.pop(session_name, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.exception(f"Unhandled OpenViking sync error for {session_name}: {e}")

    def _schedule_openviking_sync(self, session_key: SessionKey) -> None:
        """Schedule background OpenViking sync with per-session ordering."""
        session_name = session_key.safe_name()
        previous_task = self._openviking_sync_tasks.get(session_name)

        async def _runner() -> None:
            if previous_task is not None:
                try:
                    await previous_task
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Preserve the new sync attempt even if the previous one failed.
                    pass
            await self._sync_pending_messages_to_openviking(session_key)

        task = asyncio.create_task(_runner())
        self._openviking_sync_tasks[session_name] = task
        task.add_done_callback(
            lambda finished_task, name=session_name: self._on_openviking_sync_done(
                name, finished_task
            )
        )

    async def _sync_pending_messages_to_openviking(self, session_key: SessionKey) -> None:
        """Synchronize unsynced local session messages to OpenViking in the background."""
        session_name = session_key.safe_name()
        session_lock = self._session_locks.setdefault(session_name, asyncio.Lock())

        async with session_lock:
            session = self.sessions.get_or_create(
                session_key, skip_heartbeat=session_key.type == "cli"
            )
            session_metadata = session.metadata if isinstance(session.metadata, dict) else {}
            openviking_session_id = session_metadata.get("openviking_session_id")
            if not isinstance(openviking_session_id, str) or not openviking_session_id.strip():
                return
            if session_metadata.get("openviking_memory_scope") != "all":
                raise ValueError(
                    "OpenViking live session mirroring requires memory_scope='all' for "
                    f"session {session_name}"
                )

            sync_candidates: list[tuple[int, dict[str, Any]]] = []
            for index, message in enumerate(session.messages):
                if message.get("openviking_synced"):
                    continue
                if not self._message_has_openviking_payload(message):
                    message["openviking_synced"] = True
                    message.pop("openviking_sync_error", None)
                    continue
                sync_candidates.append((index, message))

            if not sync_candidates:
                session.metadata.pop("openviking_last_sync_error", None)
                await self.sessions.save(session)
                return

            sync_messages = [message for _, message in sync_candidates]
            mirror_result = await mirror_messages_to_openviking(
                session_key,
                sync_messages,
                metadata=session.metadata,
            )

            if not mirror_result.get("success", False):
                sync_error = str(mirror_result.get("error", "OpenViking sync failed"))
                session.metadata["openviking_last_sync_error"] = sync_error
                for _, message in sync_candidates:
                    message["openviking_sync_error"] = sync_error
                await self.sessions.save(session)
                logger.warning(
                    "OpenViking background sync failed for {}: {}",
                    openviking_session_id,
                    sync_error,
                )
                return

            appended_indices = set(mirror_result.get("appended_indices", []))
            sync_error = ""
            for relative_index, (_, message) in enumerate(sync_candidates):
                if relative_index in appended_indices:
                    message["openviking_synced"] = True
                    message.pop("openviking_sync_error", None)
                else:
                    sync_error = "Message was not appended to OpenViking"
                    message["openviking_sync_error"] = sync_error

            if sync_error:
                session.metadata["openviking_last_sync_error"] = sync_error
            else:
                session.metadata.pop("openviking_last_sync_error", None)

            await self.sessions.save(session)

    async def _run_agent_loop(
        self,
        messages: list[dict],
        session_key: SessionKey,
        publish_events: bool = True,
        sender_id: str | None = None,
        allow_grounded_history_reuse: bool = False,
    ) -> tuple[str | None, list[dict], dict[str, int], int]:
        """
        Run the core agent loop: call LLM, execute tools, repeat until done.

        Args:
            messages: Initial message list
            session_key: Session key for tool execution context
            publish_events: Whether to publish ITERATION/REASONING/TOOL_CALL events to the bus
            allow_grounded_history_reuse: Whether structured reuse of the latest grounded reply
                is available for this turn.

        Returns:
            tuple of (final_content, tools_used)
        """
        if self.context._is_retrieval_mode():
            if self._should_use_kb_fast_batch_path():
                return await self._run_kb_fast_batch_loop(
                    messages=messages,
                    session_key=session_key,
                    publish_events=publish_events,
                    sender_id=sender_id,
                    allow_grounded_history_reuse=allow_grounded_history_reuse,
                )
            final_content = self._build_iteration_limit_terminal_response(
                messages=messages,
                has_kb_read_evidence=False,
            )
            logger.warning(
                f"[KB_TRACE] session={session_key.safe_name()} "
                "retrieval_path=fast_batch_unavailable action=terminal_response "
                "reason=missing_openviking_search_or_read"
            )
            return final_content, [], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, 0

        return await self._run_agent_loop_classic(
            messages=messages,
            session_key=session_key,
            publish_events=publish_events,
            sender_id=sender_id,
            allow_grounded_history_reuse=allow_grounded_history_reuse,
        )

    async def _run_kb_fast_batch_loop(
        self,
        messages: list[dict],
        session_key: SessionKey,
        publish_events: bool = True,
        sender_id: str | None = None,
        allow_grounded_history_reuse: bool = False,
    ) -> tuple[str | None, list[dict], dict[str, int], int]:
        """Run the deterministic no-fallback KB path: search, batch read, answer."""
        trace_session = session_key.safe_name()
        token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        tools_used: list[dict] = []
        user_request = self._extract_user_text(messages)
        retrieval_query = user_request.strip()

        if publish_events:
            await self.bus.publish_outbound(
                OutboundMessage(
                    session_key=session_key,
                    content="Iteration 1/1",
                    event_type=OutboundEventType.ITERATION,
                )
            )

        logger.info(
            f"[KB_TRACE] session={trace_session} retrieval_path=fast_batch "
            f"search_limit={self.KB_FAST_BATCH_SEARCH_LIMIT} "
            f"read_limit={self.KB_FAST_BATCH_READ_LIMIT} "
            f"allow_grounded_history_reuse={allow_grounded_history_reuse}"
        )

        search_args = {
            "query": retrieval_query,
            "target_uri": "viking://resources/",
            "limit": self.KB_FAST_BATCH_SEARCH_LIMIT,
        }
        search_tool_id = "kb_fast_search_1"
        messages = self.context.add_assistant_message(
            messages,
            None,
            [self._tool_call_dict(search_tool_id, "openviking_search", search_args)],
        )
        await self._publish_tool_call_event(
            session_key=session_key,
            tool_name="openviking_search",
            arguments=search_args,
            publish_events=publish_events,
        )
        search_result, search_duration_ms = await self._execute_fast_batch_tool(
            tool_name="openviking_search",
            arguments=search_args,
            session_key=session_key,
            sender_id=sender_id,
        )
        messages = self.context.add_tool_result(
            messages, search_tool_id, "openviking_search", search_result
        )
        await self._publish_tool_result_event(
            session_key=session_key,
            result=search_result,
            publish_events=publish_events,
        )
        tools_used.append(
            self._tool_record(
                tool_name="openviking_search",
                arguments=search_args,
                result=search_result,
                duration_ms=search_duration_ms,
            )
        )

        plan = self._build_fast_batch_search_plan(
            search_result=search_result,
            max_documents=self.KB_FAST_BATCH_READ_LIMIT,
        )
        logger.info(
            f"[KB_TRACE] session={trace_session} retrieval_path=fast_batch "
            f"stage=search duration_ms={search_duration_ms:.1f} "
            f"search_limit={self.KB_FAST_BATCH_SEARCH_LIMIT} "
            f"total_matches={plan.total_matches if plan.total_matches is not None else 'unknown'} "
            f"candidate_doc_count={len(plan.document_uris)}"
        )

        if not plan.document_uris:
            final_content = self._build_iteration_limit_terminal_response(
                messages=messages,
                has_kb_read_evidence=False,
            )
            logger.info(
                f"[KB_TRACE] session={trace_session} retrieval_path=fast_batch "
                "evidence_status=none reason=no_concrete_documents action=terminal_response"
            )
            return final_content, tools_used, token_usage, 1

        async def read_one(index: int, uri: str):
            read_args = {
                "uri": uri,
                "level": "read",
                "include_images": True,
                "max_images": 4,
            }
            tool_id = f"kb_fast_read_{index}"
            await self._publish_tool_call_event(
                session_key=session_key,
                tool_name="openviking_read",
                arguments=read_args,
                publish_events=publish_events,
            )
            result, duration_ms = await self._execute_fast_batch_tool(
                tool_name="openviking_read",
                arguments=read_args,
                session_key=session_key,
                sender_id=sender_id,
            )
            return index, tool_id, read_args, result, duration_ms

        batch_read_start = time.time()
        read_results = await asyncio.gather(
            *(read_one(index, uri) for index, uri in enumerate(plan.document_uris, start=1))
        )
        batch_read_duration_ms = (time.time() - batch_read_start) * 1000

        read_tool_calls: list[dict[str, Any]] = []
        for _index, tool_id, read_args, _result, _duration_ms in read_results:
            read_tool_calls.append(self._tool_call_dict(tool_id, "openviking_read", read_args))
        messages = self.context.add_assistant_message(messages, None, read_tool_calls)

        concrete_read_records: list[dict[str, Any]] = []
        for index, tool_id, read_args, result, duration_ms in read_results:
            messages = self.context.add_tool_result(messages, tool_id, "openviking_read", result)
            await self._publish_tool_result_event(
                session_key=session_key,
                result=result,
                publish_events=publish_events,
            )
            record = self._tool_record(
                tool_name="openviking_read",
                arguments=read_args,
                result=result,
                duration_ms=duration_ms,
            )
            tools_used.append(record)
            evidence_ok, evidence_reason = self._classify_kb_evidence_result(
                tool_name="openviking_read",
                arguments=read_args,
                result=result,
            )
            logger.info(
                f"[KB_TRACE] session={trace_session} retrieval_path=fast_batch "
                f"stage=read index={index} duration_ms={duration_ms:.1f} "
                f"uri={read_args['uri']} evidence_ok={evidence_ok} reason={evidence_reason}"
            )
            if evidence_ok:
                concrete_read_records.append(record)

        logger.info(
            f"[KB_TRACE] session={trace_session} retrieval_path=fast_batch "
            f"stage=batch_read batch_read_count={len(read_results)} "
            f"concrete_read_count={len(concrete_read_records)} "
            f"batch_read_duration_ms={batch_read_duration_ms:.1f}"
        )

        if not concrete_read_records:
            final_content = self._build_iteration_limit_terminal_response(
                messages=messages,
                has_kb_read_evidence=False,
            )
            logger.info(
                f"[KB_TRACE] session={trace_session} retrieval_path=fast_batch "
                "evidence_status=none reason=batch_read_no_concrete_evidence "
                "action=terminal_response"
            )
            return final_content, tools_used, token_usage, 1

        evidence_selection_start = time.time()
        evidence_selection = await self._collect_fast_batch_evidence_selection(
            user_request,
            concrete_read_records,
            session_key,
            max_blocks=self.KB_FAST_BATCH_MAX_EVIDENCE_BLOCKS,
        )
        evidence_selection_duration_ms = (time.time() - evidence_selection_start) * 1000
        coverage = evidence_selection.coverage
        if coverage not in {"full", "partial", "none"}:
            coverage = "partial" if evidence_selection.sections else "none"
        evidence_status = (
            "full" if coverage == "full" else "partial" if evidence_selection.sections else "none"
        )
        logger.info(
            f"[KB_TRACE] session={trace_session} retrieval_path=fast_batch "
            f"stage=evidence_select duration_ms={evidence_selection_duration_ms:.1f} "
            f"selected_blocks={len(evidence_selection.sections)} "
            f"evidence_status={evidence_status} coverage={coverage}"
        )

        if not evidence_selection.sections:
            final_content = self._build_iteration_limit_terminal_response(
                messages=messages,
                has_kb_read_evidence=True,
            )
            logger.info(
                f"[KB_TRACE] session={trace_session} retrieval_path=fast_batch "
                "evidence_status=none reason=no_relevant_sections action=terminal_response"
            )
            return final_content, tools_used, token_usage, 1

        source_uris = self._concrete_read_uris(concrete_read_records)
        messages.append(
            {
                "role": "system",
                "content": self._build_relevant_evidence_prompt(
                    user_request,
                    evidence_selection.sections,
                    source_uri="\n".join(source_uris),
                ),
            }
        )

        answer_start = time.time()
        final_content = await self._compose_answer_from_selected_evidence(messages, session_key)
        answer_duration_ms = (time.time() - answer_start) * 1000
        logger.info(
            f"[KB_TRACE] session={trace_session} retrieval_path=fast_batch "
            f"stage=answer_generation answer_generation_duration_ms={answer_duration_ms:.1f} "
            f"chars={len(final_content or '')} evidence_status={evidence_status}"
        )

        if not final_content:
            final_content = self._build_iteration_limit_terminal_response(
                messages=messages,
                has_kb_read_evidence=True,
            )

        finalize_start_time = time.time()
        final_content = await self._finalize_kb_response(final_content, session_key, messages)
        final_content = self._normalize_final_output_text(final_content)
        logger.info(
            f"[KB_TRACE] session={trace_session} retrieval_path=fast_batch "
            f"stage=finalize finalize_response_duration_ms={(time.time() - finalize_start_time) * 1000:.1f} "
            f"chars={len(final_content or '')} evidence_status={evidence_status}"
        )

        return final_content, tools_used, token_usage, 1

    async def _run_agent_loop_classic(
        self,
        messages: list[dict],
        session_key: SessionKey,
        publish_events: bool = True,
        sender_id: str | None = None,
        allow_grounded_history_reuse: bool = False,
    ) -> tuple[str | None, list[dict], dict[str, int], int]:
        """Run the original model-planned agent loop for non-KB modes."""
        iteration = 0
        final_content = None
        tools_used: list[dict] = []
        current_turn_evidence_query: str | None = None
        token_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        has_kb_read_evidence = False
        has_sufficient_kb_evidence = False
        selected_evidence_uris: set[str] = set()
        exhausted_evidence_uris: set[str] = set()
        coverage_missing = ""
        coverage_next_query = ""
        has_grounded_history_candidate = allow_grounded_history_reuse
        trace_enabled = self.context._is_retrieval_mode()
        trace_session = session_key.safe_name()
        trace_profile = "knowledge-base" if trace_enabled else "general"

        if trace_enabled:
            logger.info(
                f"[KB_TRACE] session={trace_session} start "
                f"max_iterations={self.max_iterations} "
                f"profile={trace_profile}"
            )

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
            if has_grounded_history_candidate:
                tool_definitions = [
                    *tool_definitions,
                    self._build_grounded_history_answer_tool_definition(),
                ]
            tool_choice = self._select_tool_choice(
                iteration=iteration,
                tools=tool_definitions,
                has_sufficient_kb_evidence=has_sufficient_kb_evidence,
            )
            if trace_enabled:
                logger.info(
                    f"[KB_TRACE] session={trace_session} iteration={iteration}/{self.max_iterations} "
                    f"requested_tool_choice={tool_choice or 'auto'} "
                    f"has_concrete_kb_read_evidence={has_kb_read_evidence} "
                    f"has_sufficient_kb_evidence={has_sufficient_kb_evidence} "
                    f"has_grounded_history_candidate={has_grounded_history_candidate} "
                    f"available_tools={len(tool_definitions)}"
                )
            llm_start_time = time.time()
            response = await self.provider.chat(
                messages=messages,
                tools=tool_definitions,
                tool_choice=tool_choice,
                model=self.model,
                session_id=session_key.safe_name(),
            )
            llm_duration_ms = (time.time() - llm_start_time) * 1000
            if response.usage:
                cur_token = response.usage
                token_usage["prompt_tokens"] += cur_token["prompt_tokens"]
                token_usage["completion_tokens"] += cur_token["completion_tokens"]
                token_usage["total_tokens"] += cur_token["total_tokens"]

            if trace_enabled:
                effective_tool_choice = response.metadata.get("effective_tool_choice")
                logger.info(
                    f"[KB_TRACE] session={trace_session} iteration={iteration}/{self.max_iterations} "
                    f"llm_response tool_calls={len(response.tool_calls or [])} "
                    f"content_chars={len(response.content or '')} "
                    f"reasoning_chars={len(response.reasoning_content or '')} "
                    f"duration_ms={llm_duration_ms:.1f} "
                    f"requested_tool_choice={tool_choice or 'auto'} "
                    f"effective_tool_choice={effective_tool_choice or 'auto'}"
                )

            if response.finish_reason == "error":
                error_message = response.content or "LLM provider returned an error response"
                logger.error(
                    f"[LLM_ERROR] session={trace_session} iteration={iteration}/{self.max_iterations} "
                    f"model={self.model} requested_tool_choice={tool_choice or 'auto'} error={error_message}"
                )
                final_content = (
                    "抱歉，当前模型调用失败，暂时无法完成回答。请检查模型配置或服务端日志后重试。"
                )
                break

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
                and self.context._is_retrieval_mode()
                and not has_sufficient_kb_evidence
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
                grounded_history_calls = [
                    tool_call
                    for tool_call in response.tool_calls
                    if tool_call.name == self.GROUNDED_HISTORY_ANSWER_TOOL
                ]
                retrieval_calls = [
                    tool_call
                    for tool_call in response.tool_calls
                    if tool_call.name != self.GROUNDED_HISTORY_ANSWER_TOOL
                ]
                if (
                    has_grounded_history_candidate
                    and grounded_history_calls
                    and not retrieval_calls
                ):
                    answer = grounded_history_calls[0].arguments.get("answer")
                    if isinstance(answer, str) and answer.strip():
                        final_content = answer.strip()
                        if trace_enabled:
                            logger.info(
                                f"[KB_TRACE] session={trace_session} "
                                f"iteration={iteration}/{self.max_iterations} "
                                "final_answer_from_grounded_history "
                                f"chars={len(final_content)}"
                            )
                        break

                    has_grounded_history_candidate = False
                    messages.append(
                        {
                            "role": "system",
                            "content": (
                                "The structured grounded-history answer was empty. "
                                "Retrieve current-turn document evidence now."
                            ),
                        }
                    )
                    continue

                response.tool_calls = retrieval_calls
                # Once the model chooses retrieval, require evidence from this turn
                # instead of falling back to an older grounded reply.
                has_grounded_history_candidate = False
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
                    for tc, args in zip(response.tool_calls, args_list, strict=False)
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
                    tool_call_index = len(tools_used) + 1
                    logger.info(f"[TOOL_CALL]: {tool_call.name}({args_str[:200]})")
                    logger.info(f"[RESULT]: {str(result)[:600]}")
                    relevant_evidence_blocks: list[str] = []
                    evidence_coverage = "none"

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
                    evidence_ok, evidence_reason = self._classify_kb_evidence_result(
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                        result=result,
                    )
                    if evidence_ok:
                        if current_turn_evidence_query is None:
                            current_turn_evidence_query = await self._build_semantic_evidence_query(
                                messages, session_key
                            )
                        existing_evidence_blocks = (
                            self._collect_selected_evidence_blocks_from_prompts(
                                messages[self._current_turn_start_index(messages) :]
                            )
                        )
                        evidence_selection = (
                            await self._collect_document_evidence_selection_semantic(
                                current_turn_evidence_query,
                                [
                                    {
                                        "tool_name": tool_call.name,
                                        "args": args_str,
                                        "result": result,
                                        "execute_success": True,
                                    }
                                ],
                                session_key,
                                existing_evidence_blocks=existing_evidence_blocks,
                            )
                        )
                        evidence_blocks = evidence_selection.sections
                        if not evidence_blocks:
                            evidence_ok = False
                            evidence_reason = "openviking_read_no_relevant_section_for_user_request"
                        else:
                            relevant_evidence_blocks = evidence_blocks
                            coverage = evidence_selection.coverage
                            if coverage not in {"full", "partial"}:
                                coverage = "partial"
                            evidence_coverage = coverage
                            has_sufficient_kb_evidence = (
                                has_sufficient_kb_evidence or coverage == "full"
                            )
                            if coverage == "partial":
                                coverage_missing = evidence_selection.missing
                                coverage_next_query = evidence_selection.next_query
                    if trace_enabled:
                        logger.info(
                            f"[KB_TRACE] session={trace_session} tool_call#{tool_call_index} "
                            f"iteration={iteration}/{self.max_iterations} name={tool_call.name} "
                            f"duration_ms={tool_execute_duration:.1f} "
                            f"evidence_ok={evidence_ok} coverage={evidence_coverage} "
                            f"evidence_reason={evidence_reason}"
                        )
                        result_summary = self._summarize_tool_result_for_trace(
                            tool_call.name, tool_call.arguments, result
                        ).replace("\n", " | ")
                        logger.debug(
                            f"[KB_TRACE] session={trace_session} tool_call#{tool_call_index} "
                            f"tool_result_summary {result_summary}"
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
                    has_kb_read_evidence = has_kb_read_evidence or evidence_ok
                    if evidence_ok:
                        evidence_uri = str(tool_call.arguments.get("uri") or "").strip()
                        if evidence_uri:
                            selected_evidence_uris.add(evidence_uri)
                            if not has_sufficient_kb_evidence:
                                exhausted_evidence_uris.add(evidence_uri)
                        if relevant_evidence_blocks:
                            messages.append(
                                {
                                    "role": "system",
                                    "content": self._build_relevant_evidence_prompt(
                                        self._extract_user_text(messages),
                                        relevant_evidence_blocks,
                                        source_uri=evidence_uri,
                                    ),
                                }
                            )
                    elif evidence_reason == "openviking_read_no_relevant_section_for_user_request":
                        messages.append(
                            {
                                "role": "system",
                                "content": (
                                    "The last openviking_read result was a concrete document, "
                                    "but no section relevant to the current user request was found "
                                    f"({evidence_reason}). Continue retrieval instead of answering "
                                    "from that unrelated document text."
                                ),
                            }
                        )

                if has_sufficient_kb_evidence:
                    if trace_enabled:
                        logger.info(
                            f"[KB_TRACE] session={trace_session} iteration={iteration}/{self.max_iterations} "
                            "evidence_state=sufficient "
                            f"selected_source_count={len(selected_evidence_uris)} "
                            "action=answer"
                        )
                    messages.append(
                        {
                            "role": "system",
                            "content": self._build_answer_or_continue_prompt(
                                self._extract_user_text(messages),
                                evidence_source_count=len(selected_evidence_uris),
                            ),
                        }
                    )
                elif has_kb_read_evidence:
                    if trace_enabled:
                        logger.info(
                            f"[KB_TRACE] session={trace_session} iteration={iteration}/{self.max_iterations} "
                            "evidence_state=partial action=continue_retrieval "
                            f"selected_source_count={len(selected_evidence_uris)}"
                        )
                    messages.append(
                        {
                            "role": "system",
                            "content": self._build_partial_coverage_continue_prompt(
                                user_request=self._extract_user_text(messages),
                                missing=coverage_missing,
                                next_query=coverage_next_query,
                                exhausted_uris=exhausted_evidence_uris,
                                progress_summary=self._summarize_kb_tool_state(messages),
                            ),
                        }
                    )
                else:
                    messages.append(
                        {"role": "system", "content": self.context.build_tool_reflection_prompt()}
                    )
            else:
                if self.context._is_retrieval_mode() and not has_sufficient_kb_evidence:
                    # Grounded-history reuse requires the structured answer tool.
                    # Plain text without current-turn evidence remains insufficient.
                    has_grounded_history_candidate = False
                    if trace_enabled:
                        logger.info(
                            f"[KB_TRACE] session={trace_session} iteration={iteration}/{self.max_iterations} "
                            "evidence_state=insufficient "
                            "reason=no_concrete_openviking_read_or_bid_evidence; "
                            "action=continue_retrieval"
                        )
                    if response.content or response.reasoning_content:
                        messages = self.context.add_assistant_message(
                            messages,
                            response.content,
                            reasoning_content=response.reasoning_content,
                        )
                    messages.append(
                        {
                            "role": "system",
                            "content": self.context.build_retrieval_continue_search_prompt(
                                progress_summary=self._summarize_kb_tool_state(messages)
                            ),
                        }
                    )
                    continue
                final_content = response.content
                if trace_enabled:
                    logger.info(
                        f"[KB_TRACE] session={trace_session} iteration={iteration}/{self.max_iterations} "
                        f"final_answer_from_model chars={len(final_content or '')}"
                    )
                break

        if final_content is None or (isinstance(final_content, str) and not final_content.strip()):
            if self.context._is_retrieval_mode() and has_kb_read_evidence:
                if trace_enabled:
                    logger.info(
                        f"[KB_TRACE] session={trace_session} empty_final_with_selected_evidence "
                        "action=answer_from_selected_evidence"
                    )
                final_content = await self._compose_answer_from_selected_evidence(
                    messages,
                    session_key,
                )
                if final_content and trace_enabled:
                    logger.info(
                        f"[KB_TRACE] session={trace_session} selected_evidence_answer "
                        f"chars={len(final_content)}"
                    )

        if final_content is None or (isinstance(final_content, str) and not final_content.strip()):
            if iteration >= self.max_iterations:
                if trace_enabled:
                    trace_reason = (
                        "iteration_limit_with_concrete_kb_read_evidence"
                        if has_kb_read_evidence
                        else "no_concrete_kb_read_evidence"
                    )
                    logger.info(
                        f"[KB_TRACE] session={trace_session} iteration_limit action=terminal_response "
                        f"reason={trace_reason} tool_messages={self._count_tool_messages(messages)}"
                    )
                final_content = self._build_iteration_limit_terminal_response(
                    messages=messages,
                    has_kb_read_evidence=has_kb_read_evidence,
                )
            else:
                final_content = "I've completed processing but have no response to give."

        if final_content:
            finalize_start_time = time.time()
            final_content = await self._finalize_kb_response(final_content, session_key, messages)
            final_content = self._normalize_final_output_text(final_content)
            if trace_enabled:
                logger.info(
                    f"[KB_TRACE] session={trace_session} finalize_response "
                    f"duration_ms={(time.time() - finalize_start_time) * 1000:.1f} "
                    f"chars={len(final_content or '')}"
                )

        return final_content, tools_used, token_usage, iteration

    def _classify_kb_evidence_result(
        self, tool_name: str, arguments: dict | None, result: str
    ) -> tuple[bool, str]:
        """Classify whether a tool result is concrete evidence and explain the reason."""
        if not self.context._is_retrieval_mode():
            return False, "not_retrieval_mode"

        result_text = result if isinstance(result, str) else str(result or "")
        error_reason = self._classify_tool_error_result(result_text)
        if error_reason:
            return False, error_reason

        if tool_name != "openviking_read":
            if tool_name in {"openviking_search", "openviking_glob", "openviking_list"}:
                return False, f"{tool_name}_returns_candidates_only_requires_openviking_read"
            return False, "tool_result_is_not_document_read_evidence"
        if not isinstance(arguments, dict):
            return False, "openviking_read_arguments_not_dict"
        if arguments.get("level", "abstract") != "read":
            return False, f"openviking_read_level_is_{arguments.get('level', 'abstract')}"

        uri = str(arguments.get("uri", "") or "")
        if not uri or is_summary_uri(uri) or is_generic_scope_summary_uri(uri):
            if not uri:
                return False, "openviking_read_missing_uri"
            if is_generic_scope_summary_uri(uri):
                return False, "openviking_read_uri_is_generic_scope_summary_not_concrete_doc"
            return False, "openviking_read_uri_is_summary_not_concrete_doc"

        if not result_text.strip():
            return False, "openviking_read_returned_empty_result"
        if "不能直接执行 level='read'" in result_text:
            return False, "openviking_read_target_is_directory_requires_child_uri"
        if "下没有可读取的正文文件" in result_text:
            return False, "openviking_read_directory_has_no_readable_text"
        return True, "openviking_read_level_read_concrete_uri_with_non_empty_result"

    @staticmethod
    def _classify_tool_error_result(result_text: str) -> str | None:
        """Return a stable trace reason when a tool result is an execution error."""
        if not result_text:
            return None

        if "All connection attempts failed" in result_text:
            return "tool_connection_failed"
        if "ConnectError" in result_text or "Connection refused" in result_text:
            return "tool_connection_failed"
        if result_text.startswith("Error executing "):
            return "tool_execution_error"
        if result_text.startswith("Error searching Viking with glob"):
            return "openviking_glob_error"
        if result_text.startswith("Error searching Viking with grep"):
            return "openviking_grep_error"
        if result_text.startswith("Error searching Viking"):
            return "openviking_search_error"
        if result_text.startswith("Error listing Viking resources"):
            return "openviking_list_error"
        if result_text.startswith("Error reading from Viking"):
            return "openviking_read_error"
        if result_text.startswith("Error:"):
            return "tool_returned_error"
        return None

    @classmethod
    def _summarize_tool_result_for_trace(
        cls,
        tool_name: str,
        arguments: dict[str, Any] | None,
        result: Any,
        max_items: int = 8,
        max_preview_chars: int = 260,
    ) -> str:
        """Build a compact, grep-friendly trace summary of returned tool fragments."""
        result_text = result if isinstance(result, str) else str(result or "")
        args = arguments if isinstance(arguments, dict) else {}
        lines = [f"result_chars={len(result_text)}"]

        if not result_text.strip():
            lines.append("fragments=0")
            return "\n".join(lines)

        if tool_name == "openviking_search":
            limit_match = re.search(r"Requested limit:\s*(.+?)\s*$", result_text, re.MULTILINE)
            if limit_match:
                lines.append(
                    f"requested_limit={cls._truncate_trace_text(limit_match.group(1), 80)}"
                )
            total_match = re.search(r"Total matches:\s*(\d+)", result_text)
            if total_match:
                lines.append(f"total_matches={total_match.group(1)}")
            entries = cls._extract_search_entries_for_trace(result_text, max_items=max_items)
            lines.append(f"search_entries={len(entries)}")
            lines.extend(entries)
            if entries:
                return "\n".join(lines)

        if tool_name in {"openviking_glob", "openviking_list"}:
            uris = cls._extract_viking_uris_for_trace(result_text, max_items=max_items)
            lines.append(f"candidate_uris={len(uris)}")
            lines.extend(f"{index}. uri={uri}" for index, uri in enumerate(uris, start=1))
            if uris:
                return "\n".join(lines)

        if tool_name == "openviking_read":
            uri = str(args.get("uri") or "")
            level = str(args.get("level", "abstract"))
            image_refs = len(re.findall(r"!\[[^\]]*\]\([^)]+\)", result_text))
            lines.append(f"read_uri={uri or '(missing)'} level={level} image_refs={image_refs}")

        fragments = cls._extract_text_fragments_for_trace(
            result_text,
            max_items=max_items,
            max_preview_chars=max_preview_chars,
        )
        lines.append(f"fragments={len(fragments)}")
        lines.extend(
            f"{index}. preview={fragment}" for index, fragment in enumerate(fragments, start=1)
        )
        return "\n".join(lines)

    @classmethod
    def _extract_search_entries_for_trace(cls, text: str, max_items: int = 8) -> list[str]:
        """Extract result entries from formatted OpenViking search output."""
        source_lines = text.splitlines()
        entries: list[str] = []

        for index, line in enumerate(source_lines):
            match = re.match(r"\s*(\d+)\.\s+\[([^\]]+)\]\s+(.+?)\s*$", line)
            if not match:
                continue

            reason = ""
            for next_line in source_lines[index + 1 : index + 4]:
                reason_match = re.match(r"\s*Match reason:\s*(.+?)\s*$", next_line)
                if reason_match:
                    reason = cls._truncate_trace_text(reason_match.group(1), 180)
                    break

            entry = f"{len(entries) + 1}. kind={match.group(2)} uri={match.group(3)}"
            if reason:
                entry += f" match_reason={reason}"
            entries.append(entry)
            if len(entries) >= max_items:
                break

        return entries

    @staticmethod
    def _extract_viking_uris_for_trace(text: str, max_items: int = 8) -> list[str]:
        """Extract unique viking:// URIs from a tool result."""
        uris: list[str] = []
        seen: set[str] = set()
        for match in re.finditer(r"viking://[^\s)]+", text):
            uri = match.group(0).rstrip(".,;:")
            if uri in seen:
                continue
            seen.add(uri)
            uris.append(uri)
            if len(uris) >= max_items:
                break
        return uris

    @classmethod
    def _extract_text_fragments_for_trace(
        cls, text: str, max_items: int = 8, max_preview_chars: int = 260
    ) -> list[str]:
        """Extract readable fragment previews from a tool result."""
        normalized = text.replace("\r\n", "\n").strip()
        blocks = [
            block.strip() for block in re.split(r"\n\s*\n+", normalized) if block and block.strip()
        ]
        if len(blocks) <= 1:
            blocks = [line.strip() for line in normalized.splitlines() if line.strip()]

        fragments: list[str] = []
        for block in blocks:
            compact = re.sub(r"\s+", " ", block).strip()
            if not compact or compact == "---" or compact.startswith("!["):
                continue
            fragments.append(cls._truncate_trace_text(compact, max_preview_chars))
            if len(fragments) >= max_items:
                break
        return fragments

    @staticmethod
    def _truncate_trace_text(text: str, max_chars: int) -> str:
        """Trim a trace field while keeping it single-line."""
        compact = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(compact) <= max_chars:
            return compact
        return f"{compact[: max_chars - 3]}..."

    @staticmethod
    def _count_tool_messages(messages: list[dict]) -> int:
        """Count tool result messages in the agent message history."""
        return sum(1 for message in messages if message.get("role") == "tool")

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

    @staticmethod
    def _extract_message_text(content: Any) -> str:
        """Extract text from an OpenAI-style message content payload."""
        if isinstance(content, str):
            return content.strip()

        texts: list[str] = []
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = str(block.get("text") or "").strip()
                if text and not text.startswith("image saved to "):
                    texts.append(text)

        return "\n".join(texts).strip()

    @classmethod
    def _latest_user_message_index(cls, messages: list[dict]) -> int | None:
        """Return the latest user message index, ignoring empty user-memory payloads."""
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if message.get("role") != "user":
                continue
            if cls._extract_message_text(message.get("content")):
                return index
        return None

    @classmethod
    def _current_turn_start_index(cls, messages: list[dict]) -> int:
        """Return the index immediately after the latest user message."""
        latest_user_index = cls._latest_user_message_index(messages)
        return 0 if latest_user_index is None else latest_user_index + 1

    @classmethod
    def _extract_user_text(cls, messages: list[dict]) -> str:
        """Extract the latest user-authored text for the current turn."""
        latest_user_index = cls._latest_user_message_index(messages)
        if latest_user_index is None:
            return ""
        return cls._extract_message_text(messages[latest_user_index].get("content"))

    @classmethod
    def _is_user_memory_payload(cls, text: str) -> bool:
        """Detect synthetic user-memory messages inserted before the real user turn."""
        stripped = str(text or "").lstrip()
        return stripped.startswith("## Current Time:") or stripped.startswith(
            "## Long term memory about this conversation."
        )

    @classmethod
    def _recent_conversation_text_for_followup(
        cls, messages: list[dict], latest_user_index: int, *, max_messages: int = 4
    ) -> str:
        """Collect recent user/assistant text, excluding tool chatter and synthetic memory."""
        snippets: list[str] = []
        for message in reversed(messages[:latest_user_index]):
            role = message.get("role")
            if role not in {"user", "assistant"}:
                continue
            if role == "assistant" and message.get("tool_calls"):
                continue
            text = cls._extract_message_text(message.get("content"))
            if not text or cls._is_user_memory_payload(text):
                continue
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) > 160:
                text = f"{text[:157]}..."
            snippets.append(text)
            if len(snippets) >= max_messages:
                break
        snippets.reverse()
        return "\n".join(snippets).strip()

    @classmethod
    def _build_evidence_query_rewrite_prompt(cls, messages: list[dict]) -> str:
        """Build a compact prompt that asks the model to resolve follow-up ellipsis."""
        latest_user_index = cls._latest_user_message_index(messages)
        latest_text = cls._extract_user_text(messages)
        context_text = ""
        if latest_user_index is not None:
            context_text = cls._recent_conversation_text_for_followup(
                messages,
                latest_user_index,
                max_messages=6,
            )
        return (
            "Rewrite the latest user message into one standalone document-retrieval query.\n"
            "Use recent chat context only to resolve pronouns or omitted nouns. "
            "If the latest message is already standalone, return it unchanged. "
            "Do not answer the question. Do not add facts that are not implied by the chat. "
            "Return only the rewritten query text.\n\n"
            f"Recent chat context:\n{context_text or '(none)'}\n\n"
            f"Latest user message:\n{latest_text}"
        )

    async def _build_semantic_evidence_query(
        self, messages: list[dict], session_key: SessionKey
    ) -> str:
        """Ask the model to resolve contextual turns without local heuristics."""
        latest_user_text = self._extract_user_text(messages)
        latest_user_index = self._latest_user_message_index(messages)
        if latest_user_index is None:
            return latest_user_text

        recent_context = self._recent_conversation_text_for_followup(
            messages,
            latest_user_index,
            max_messages=6,
        )
        if not recent_context:
            return latest_user_text

        try:
            response = await self.provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You rewrite chat follow-ups into concise search queries for document "
                            "retrieval. Return only the rewritten query."
                        ),
                    },
                    {
                        "role": "user",
                        "content": self._build_evidence_query_rewrite_prompt(messages),
                    },
                ],
                model=self.fast_model,
                max_tokens=128,
                temperature=0,
                session_id=f"{session_key.safe_name()}:kb-query-rewrite",
            )
        except Exception as exc:
            logger.debug(f"[KB_TRACE] query rewrite unavailable; using latest user text: {exc}")
            return latest_user_text

        rewritten = str(response.content or "").strip()
        rewritten = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", rewritten).strip()
        if not rewritten:
            return latest_user_text
        if len(rewritten) > 240:
            rewritten = rewritten[:240].strip()
        return rewritten

    @classmethod
    def _detect_reply_language(cls, messages: list[dict]) -> str:
        """Infer a small set of reply languages from the user's latest text."""
        user_text = cls._extract_user_text(messages)
        if re.search(r"[\u3040-\u30ff\u31f0-\u31ff\uff66-\uff9f]", user_text):
            return "ja"
        if re.search(r"[\u4e00-\u9fff]", user_text):
            return "zh-CN"
        return "en"

    def _build_iteration_limit_terminal_response(
        self, messages: list[dict], has_kb_read_evidence: bool
    ) -> str:
        """Return the terminal response when retrieval hits the iteration limit."""
        if not self.context._is_retrieval_mode():
            return f"Reached {self.max_iterations} iterations without completion."

        language = self._detect_reply_language(messages)
        if has_kb_read_evidence:
            responses = {
                "zh-CN": "抱歉，我暂时还没能根据现有资料整理出明确答复。需要的话，您可以告诉我更具体的文档范围、模块或参数点。",
                "ja": "申し訳ありません。現在の資料だけでは明確な回答をまとめきれませんでした。必要であれば、対象の文書範囲やモジュール、確認したい項目をもう少し具体的に教えてください。",
                "en": "Sorry, I still couldn't produce a clear answer from the current materials. If helpful, you can narrow the scope to a document, module, or parameter.",
            }
        else:
            responses = {
                "zh-CN": "抱歉，我暂时没有在当前知识库中找到足够依据来回答这个问题。需要的话，您可以进一步缩小范围，比如具体文档、模块、流程或参数点。",
                "ja": "申し訳ありません。現在のナレッジベースでは、この質問を明確に裏付ける情報を見つけられませんでした。必要であれば、対象の文書、モジュール、手順、または確認したい仕様をもう少し具体的に教えてください。",
                "en": "Sorry, I couldn't find enough supporting information in the current knowledge base to answer this clearly. If helpful, you can narrow it down to a specific document, module, process, or parameter.",
            }
        return responses.get(language, responses["en"])

    async def _compose_answer_from_selected_evidence(
        self,
        messages: list[dict],
        session_key: SessionKey,
    ) -> str | None:
        """Answer from already selected evidence without running more retrieval tools."""
        current_turn_messages = messages[self._current_turn_start_index(messages) :]
        evidence_blocks = self._collect_selected_evidence_blocks_from_prompts(
            current_turn_messages,
        )
        if not evidence_blocks:
            return None

        final_messages = [
            {
                "role": "system",
                "content": self.context.build_retrieval_final_response_system_prompt(),
            },
            {
                "role": "user",
                "content": (
                    "Relevant evidence has already been selected. Answer the user directly "
                    "using only that evidence. "
                    "If it supports only part of the request, answer that part and briefly say "
                    "the current documentation does not cover the remaining details.\n\n"
                    f"{self._build_relevant_evidence_prompt(self._extract_user_text(messages), evidence_blocks)}"
                ),
            },
        ]
        try:
            response = await self.provider.chat(
                messages=final_messages,
                model=self.model,
                session_id=f"{session_key.safe_name()}:kb-selected-evidence-answer",
            )
        except Exception as exc:
            logger.debug(f"[KB_TRACE] selected evidence answer failed: {exc}")
            return None
        return str(response.content or "").strip() or None

    @staticmethod
    def _build_answer_or_continue_prompt(user_request: str, *, evidence_source_count: int) -> str:
        """Prompt the model to answer from semantically sufficient evidence."""
        return (
            "A separate semantic coverage assessment found that the selected document evidence fully "
            "covers the current request. Answer directly using only the selected evidence. Do not add "
            "facts, explanations, examples, causes, effects, or procedures that the evidence does not "
            "explicitly state. "
            f"Selected evidence source count: {evidence_source_count}.\n\n"
            f"Current user request:\n{user_request.strip()}"
        )

    def _build_partial_coverage_continue_prompt(
        self,
        *,
        user_request: str,
        missing: str,
        next_query: str,
        exhausted_uris: set[str],
        progress_summary: str,
    ) -> str:
        """Require another retrieval step after semantic coverage is only partial."""
        exhausted = "\n".join(f"- {uri}" for uri in sorted(exhausted_uris)) or "(none)"
        return (
            "A separate semantic coverage assessment found that the selected evidence is relevant but "
            "does not fully cover the current request. Do not answer yet. Call the next retrieval tool "
            "directly. Do not reread an exhausted URI unless the tool arguments request materially "
            "different content such as document images.\n\n"
            f"Current user request:\n{user_request.strip()}\n\n"
            f"Unsupported or missing scope:\n{missing or '(not specified)'}\n\n"
            f"Suggested next search query:\n{next_query or '(derive one semantically)'}\n\n"
            f"Exhausted URIs for this request:\n{exhausted}\n\n"
            f"Retrieval progress:\n{progress_summary}"
        )

    def _select_tool_choice(
        self,
        iteration: int,
        tools: list[dict] | None,
        has_sufficient_kb_evidence: bool,
    ) -> str | None:
        """Select tool-choice mode for the current LLM turn."""
        if self.context._is_retrieval_mode() and not has_sufficient_kb_evidence and tools:
            return "required"
        return None

    @classmethod
    def _latest_assistant_reply_has_document_evidence(cls, session: Any) -> bool:
        """Whether the latest visible assistant reply followed a concrete document read."""
        for message in reversed(getattr(session, "messages", [])):
            if message.get("skip_history"):
                continue
            if message.get("role") != "assistant":
                return False
            tools_used = message.get("tools_used")
            return isinstance(tools_used, list) and any(
                isinstance(tool, dict) and cls._is_concrete_read_tool_record(tool)
                for tool in tools_used
            )
        return False

    @classmethod
    def _build_grounded_history_reuse_prompt(cls) -> str:
        """Authorize a narrow direct-answer path from a verified grounded reply."""
        return (
            "Runtime context confirms that the most recent assistant reply was produced after "
            "reading a concrete document. "
            "It may be reused as evidence only for the current request.\n"
            f"- If that reply directly and completely answers the current request, call "
            f"{cls.GROUNDED_HISTORY_ANSWER_TOOL} with the complete user-facing final answer.\n"
            "- If the request asks for newer, broader, more detailed, or different information, "
            "or the prior reply is incomplete or conflicting, call the next retrieval tool now.\n"
            "- Choose exactly one path. Do not output a prose-only decision or progress note."
        )

    @classmethod
    def _build_grounded_history_answer_tool_definition(cls) -> dict[str, Any]:
        """Build the virtual tool used for structured grounded-history reuse."""
        return {
            "type": "function",
            "function": {
                "name": cls.GROUNDED_HISTORY_ANSWER_TOOL,
                "description": (
                    "Return the complete final answer using only the latest runtime-verified "
                    "grounded assistant reply. Use this only when it directly and completely "
                    "answers the current request."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "answer": {
                            "type": "string",
                            "description": "The complete user-facing final answer.",
                        }
                    },
                    "required": ["answer"],
                    "additionalProperties": False,
                },
            },
        }

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

            if name == AgentLoop.GROUNDED_HISTORY_ANSWER_TOOL:
                continue
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

    @staticmethod
    def _normalize_final_output_text(content: str | None) -> str | None:
        """Collapse repeated blank lines in final output while preserving fenced code blocks."""
        if not isinstance(content, str):
            return content

        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
        if not normalized.strip():
            return normalized.strip()

        lines = normalized.split("\n")
        result_lines: list[str] = []
        blank_seen = False
        in_fenced_block = False

        for line in lines:
            if re.match(r"^\s*```", line):
                result_lines.append(line)
                blank_seen = False
                in_fenced_block = not in_fenced_block
                continue

            if in_fenced_block:
                result_lines.append(line)
                continue

            if not line.strip():
                if result_lines and not blank_seen:
                    result_lines.append("")
                blank_seen = True
                continue

            result_lines.append(line.rstrip())
            blank_seen = False

        while result_lines and result_lines[-1] == "":
            result_lines.pop()

        return "\n".join(result_lines)

    @classmethod
    def _clean_section_title(cls, title: str | None, *, keep_number: bool = False) -> str:
        """Remove Markdown/bold markup and optional list numbering from a heading."""
        text = str(title or "").strip()
        text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text)
        text = text.replace("**", "")
        text = re.sub(r"\s+", " ", text).strip()
        if not keep_number:
            text = re.sub(
                r"^(?:第[一二三四五六七八九十百千]+[章节节、]\s*|"
                r"[一二三四五六七八九十]+[、.．]\s*|"
                r"\d+(?:\.\d+){0,4}\s*)",
                "",
                text,
            ).strip()
        return text

    @classmethod
    def _is_toc_heading_candidate(cls, line: str, previous_lines: list[str]) -> bool:
        """Detect imported TOC entries such as '2.1宾客状态3'."""
        stripped = line.strip().replace("**", "")
        if not re.match(r"^\d+(?:\.\d+){0,4}[\u4e00-\u9fffA-Za-z]+[0-9]+$", stripped):
            return False
        recent = [item.strip().replace("**", "") for item in previous_lines[-5:] if item.strip()]
        return any(item in {"目录", "目 录", "contents", "content"} for item in recent)

    @classmethod
    def _heading_level(cls, line: str) -> int:
        """Infer a comparable section level from Markdown or numbered headings."""
        markdown_match = re.match(r"^\s{0,3}(#{1,6})\s+", line)
        if markdown_match:
            return len(markdown_match.group(1))

        cleaned = line.strip().replace("**", "")
        number_match = re.match(r"^(\d+(?:\.\d+){0,4})", cleaned)
        if number_match:
            return len(number_match.group(1).split("."))
        if re.match(
            r"^(?:第[一二三四五六七八九十百千]+[章节节、]|[一二三四五六七八九十]+[、.．])", cleaned
        ):
            return 1
        return 1

    @classmethod
    def _is_section_heading_line(cls, line: str, previous_lines: list[str]) -> bool:
        """Return True for Markdown and imported numbered headings."""
        if not line or not line.strip():
            return False
        if cls.MARKDOWN_HEADING_RE.match(line):
            return True
        stripped = line.strip()
        if len(stripped) > 120:
            return False
        if cls._is_toc_heading_candidate(stripped, previous_lines):
            return False
        return bool(cls.NUMBERED_HEADING_RE.match(stripped))

    @classmethod
    def _split_markdown_sections(cls, content: str | None) -> list[dict[str, Any]]:
        """Split document text into hierarchical sections with child subsections included."""
        if not isinstance(content, str) or not content.strip():
            return []

        lines = content.replace("\r\n", "\n").replace("\r", "\n").splitlines()
        headings: list[dict[str, Any]] = []
        previous_lines: list[str] = []
        for index, line in enumerate(lines):
            if cls._is_section_heading_line(line, previous_lines):
                headings.append(
                    {
                        "index": index,
                        "line": line.strip(),
                        "level": cls._heading_level(line),
                    }
                )
            previous_lines.append(line)

        if not headings:
            block = content.strip()
            return [
                {
                    "title": "",
                    "level": 1,
                    "text": block,
                    "body": block,
                    "source_index": 0,
                }
            ]

        sections: list[dict[str, Any]] = []
        for heading_index, heading in enumerate(headings):
            end = len(lines)
            for next_heading in headings[heading_index + 1 :]:
                if next_heading["level"] <= heading["level"]:
                    end = next_heading["index"]
                    break
            start = heading["index"]
            text = "\n".join(lines[start:end]).strip()
            body = "\n".join(lines[start + 1 : end]).strip()
            if not text:
                continue
            sections.append(
                {
                    "title": heading["line"],
                    "level": heading["level"],
                    "text": text,
                    "body": body,
                    "source_index": start,
                }
            )
        return sections

    @classmethod
    def _build_section_selection_prompt(
        cls,
        user_request: str,
        sections: list[dict[str, Any]],
        *,
        max_sections: int,
        existing_evidence_blocks: list[str] | None = None,
    ) -> str:
        """Build a compact section-selection and coverage prompt."""
        section_texts: list[str] = []
        for index, section in enumerate(sections, start=1):
            text = cls._prepare_text_block_for_rewrite(str(section.get("text") or ""))
            if len(text) > 900:
                text = f"{text[:900]}..."
            section_texts.append(f"[{index}]\n{text}")

        existing_evidence = "\n\n".join(existing_evidence_blocks or [])
        existing_evidence_prompt = (
            f"Existing selected evidence from earlier reads:\n{existing_evidence}\n\n"
            if existing_evidence
            else ""
        )
        return (
            "Select the document sections that directly support answering the user request, then "
            "judge the coverage of the existing evidence plus the selected sections.\n"
            "Return only JSON in this exact shape: "
            '{"sections":[1,2],"coverage":"full","missing":"","next_query":""}.\n'
            "coverage must be full, partial, or none. Use full only when every requested aspect is "
            "explicitly supported. Use partial when the text is relevant but does not support the "
            "requested depth, explanation, process, causes, effects, examples, or other requested "
            "scope. Use none when no selected text answers the request.\n"
            "For partial coverage, describe the unsupported aspect in missing and provide one concise, "
            "standalone document-search query in next_query. Do not answer the question or infer facts. "
            f"Select at most {max_sections} sections.\n\n"
            f"User request:\n{user_request}\n\n"
            f"{existing_evidence_prompt}"
            "Candidate sections:\n" + "\n\n".join(section_texts)
        )

    @staticmethod
    def _parse_section_selection_indexes(selection_text: str, max_index: int) -> list[int]:
        """Parse selected section indexes from JSON or plain-number model output."""
        indexes, _is_valid = AgentLoop._parse_section_selection_response(
            selection_text,
            max_index,
        )
        return indexes

    @staticmethod
    def _parse_section_selection_response(
        selection_text: str,
        max_index: int,
    ) -> tuple[list[int], bool]:
        """Parse selected section indexes and whether the model output was usable."""
        text = str(selection_text or "").strip()
        if not text:
            return [], False
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()

        indexes: list[int] = []
        is_valid = False
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, dict):
            raw_indexes = parsed.get("sections")
            if isinstance(raw_indexes, list):
                is_valid = True
                for item in raw_indexes:
                    try:
                        indexes.append(int(item))
                    except (TypeError, ValueError):
                        continue
        elif isinstance(parsed, list):
            is_valid = True
            for item in parsed:
                try:
                    indexes.append(int(item))
                except (TypeError, ValueError):
                    continue

        if not indexes and parsed is None:
            indexes = [int(match.group(0)) for match in re.finditer(r"\d+", text)]
            is_valid = bool(indexes)

        selected: list[int] = []
        seen: set[int] = set()
        for index in indexes:
            if 1 <= index <= max_index and index not in seen:
                seen.add(index)
                selected.append(index)
        return selected, is_valid

    @staticmethod
    def _parse_evidence_coverage_response(selection_text: str) -> tuple[str, str, str]:
        """Parse semantic coverage metadata from a section-selection response."""
        text = str(selection_text or "").strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I).strip()
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return "unknown", "", ""
        if not isinstance(parsed, dict):
            return "unknown", "", ""

        coverage = str(parsed.get("coverage") or "").strip().lower()
        if coverage not in {"full", "partial", "none"}:
            coverage = "unknown"
        missing = re.sub(r"\s+", " ", str(parsed.get("missing") or "")).strip()
        next_query = re.sub(r"\s+", " ", str(parsed.get("next_query") or "")).strip()
        return coverage, missing[:500], next_query[:240]

    async def _select_relevant_markdown_evidence_semantic(
        self,
        user_request: str,
        content: str,
        session_key: SessionKey,
        *,
        max_sections: int = 3,
        existing_evidence_blocks: list[str] | None = None,
    ) -> _SemanticEvidenceSelection:
        """Select relevant sections and assess whether they fully cover the request."""
        sections = self._split_markdown_sections(content)
        if not sections or not str(user_request or "").strip():
            return _SemanticEvidenceSelection(sections=[], coverage="none")

        try:
            response = await self.provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a strict document-evidence selector and coverage assessor. "
                            "Select only explicit evidence, distinguish relevance from completeness, "
                            "and return JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": self._build_section_selection_prompt(
                            user_request,
                            sections,
                            max_sections=max_sections,
                            existing_evidence_blocks=existing_evidence_blocks,
                        ),
                    },
                ],
                model=self.fast_model,
                max_tokens=256,
                temperature=0,
                session_id=f"{session_key.safe_name()}:kb-section-select",
            )
            indexes, _ = self._parse_section_selection_response(
                response.content or "",
                len(sections),
            )
            coverage, missing, next_query = self._parse_evidence_coverage_response(
                response.content or ""
            )
        except Exception as exc:
            logger.debug(f"[KB_TRACE] semantic evidence selection failed: {exc}")
            return _SemanticEvidenceSelection(sections=[], coverage="unknown")

        selected: list[str] = []
        seen: set[str] = set()
        for index in indexes[:max_sections]:
            text = str(sections[index - 1].get("text") or "").strip()
            cleaned = self._prepare_text_block_for_rewrite(text)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            selected.append(text)

        if not selected:
            coverage = "none"
        return _SemanticEvidenceSelection(
            sections=selected,
            coverage=coverage,
            missing=missing,
            next_query=next_query,
        )

    async def _select_relevant_markdown_sections_semantic(
        self,
        user_request: str,
        content: str,
        session_key: SessionKey,
        *,
        max_sections: int = 3,
    ) -> list[str]:
        """Select relevant document sections with a model."""
        selection = await self._select_relevant_markdown_evidence_semantic(
            user_request,
            content,
            session_key,
            max_sections=max_sections,
        )
        return selection.sections

    @classmethod
    def _build_fast_batch_evidence_selection_prompt(
        cls,
        user_request: str,
        candidate_sections: list[dict[str, str]],
        *,
        max_blocks: int,
    ) -> str:
        """Build one cross-document evidence selector prompt for the fast batch path."""
        candidates: list[str] = []
        for index, candidate in enumerate(candidate_sections, start=1):
            text = cls._prepare_text_block_for_rewrite(candidate.get("text", ""))
            if len(text) > 800:
                text = f"{text[:800]}..."
            candidates.append(f"[{index}] Source URI: {candidate.get('uri', '')}\n{text}")

        return (
            "Select the minimum document evidence sections needed to answer the user request "
            "from the candidate sections below, then judge overall coverage.\n"
            "Return only JSON in this exact shape: "
            '{"sections":[1,2],"coverage":"full","missing":"","next_query":""}.\n'
            "coverage must be full, partial, or none. Use full only when every requested aspect is "
            "explicitly supported by the selected sections. Use partial when some useful evidence "
            "exists but the requested scope is not fully supported. Use none when no selected text "
            "answers the request. Do not infer facts. Do not answer the question. "
            f"Select at most {max_blocks} sections.\n\n"
            f"User request:\n{user_request.strip()}\n\n"
            "Candidate sections:\n"
            + "\n\n".join(candidates)
        )

    async def _collect_fast_batch_evidence_selection(
        self,
        user_request: str,
        tools_used: list[dict[str, Any]],
        session_key: SessionKey,
        *,
        max_blocks: int,
    ) -> _SemanticEvidenceSelection:
        """Select evidence once across all batch-read documents."""
        candidate_sections: list[dict[str, str]] = []
        seen_candidates: set[str] = set()
        max_candidates = max(max_blocks * 4, max_blocks)
        for tool in tools_used:
            if not self._is_concrete_read_tool_record(tool):
                continue
            args = self._parse_tool_args(tool.get("args"))
            uri = str(args.get("uri") or "").strip()
            for section in self._split_markdown_sections(str(tool.get("result") or ""))[:10]:
                raw_text = str(section.get("text") or "").strip()
                cleaned = self._prepare_text_block_for_rewrite(raw_text)
                if not cleaned or cleaned in seen_candidates:
                    continue
                seen_candidates.add(cleaned)
                candidate_sections.append({"uri": uri, "text": raw_text, "cleaned": cleaned})
                if len(candidate_sections) >= max_candidates:
                    break
            if len(candidate_sections) >= max_candidates:
                break

        if not candidate_sections or not str(user_request or "").strip():
            return _SemanticEvidenceSelection(sections=[], coverage="none")

        try:
            response = await self.provider.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a strict cross-document evidence selector and coverage assessor. "
                            "Select only explicit evidence and return JSON only."
                        ),
                    },
                    {
                        "role": "user",
                        "content": self._build_fast_batch_evidence_selection_prompt(
                            user_request,
                            candidate_sections,
                            max_blocks=max_blocks,
                        ),
                    },
                ],
                model=self.fast_model,
                max_tokens=384,
                temperature=0,
                session_id=f"{session_key.safe_name()}:kb-fast-evidence-select",
            )
            indexes, _is_valid = self._parse_section_selection_response(
                response.content or "",
                len(candidate_sections),
            )
            coverage, missing, next_query = self._parse_evidence_coverage_response(
                response.content or ""
            )
        except Exception as exc:
            logger.debug(f"[KB_TRACE] fast batch evidence selection failed: {exc}")
            return _SemanticEvidenceSelection(sections=[], coverage="unknown")

        selected: list[str] = []
        seen_selected: set[str] = set()
        for index in indexes[:max_blocks]:
            candidate = candidate_sections[index - 1]
            cleaned = candidate["cleaned"]
            if cleaned in seen_selected:
                continue
            seen_selected.add(cleaned)
            selected.append(cleaned)

        if not selected:
            coverage = "none"
        return _SemanticEvidenceSelection(
            sections=selected,
            coverage=coverage,
            missing=missing,
            next_query=next_query,
        )

    @classmethod
    def _concrete_read_uris(cls, tools_used: list[dict[str, Any]]) -> list[str]:
        """Return concrete openviking_read URIs from tool records."""
        uris: list[str] = []
        seen: set[str] = set()
        for tool in tools_used:
            if not cls._is_concrete_read_tool_record(tool):
                continue
            args = cls._parse_tool_args(tool.get("args"))
            uri = str(args.get("uri") or "").strip()
            if uri and uri not in seen:
                seen.add(uri)
                uris.append(uri)
        return uris

    @staticmethod
    def _parse_tool_args(raw_args: Any) -> dict[str, Any]:
        """Parse tool args stored as JSON text in tests/session history."""
        if isinstance(raw_args, dict):
            return raw_args
        if not isinstance(raw_args, str) or not raw_args.strip():
            return {}
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @classmethod
    def _is_concrete_read_tool_record(cls, tool_record: dict[str, Any]) -> bool:
        """Whether a stored tool record is a successful concrete openviking_read."""
        if tool_record.get("tool_name") != "openviking_read":
            return False
        result = tool_record.get("result")
        if (
            not isinstance(result, str)
            or not result.strip()
            or cls._classify_tool_error_result(result)
        ):
            return False
        args = cls._parse_tool_args(tool_record.get("args"))
        uri = str(args.get("uri") or "").strip()
        level = str(args.get("level", "abstract") or "abstract")
        return bool(
            uri
            and level == "read"
            and not is_summary_uri(uri)
            and not is_generic_scope_summary_uri(uri)
        )

    async def _collect_document_evidence_blocks_semantic(
        self,
        user_request: str,
        tools_used: list[dict[str, Any]],
        session_key: SessionKey,
        *,
        max_blocks: int = 3,
    ) -> list[str]:
        """Collect section-scoped evidence using semantic section selection first."""
        selection = await self._collect_document_evidence_selection_semantic(
            user_request,
            tools_used,
            session_key,
            max_blocks=max_blocks,
        )
        return selection.sections

    async def _collect_document_evidence_selection_semantic(
        self,
        user_request: str,
        tools_used: list[dict[str, Any]],
        session_key: SessionKey,
        *,
        max_blocks: int = 3,
        existing_evidence_blocks: list[str] | None = None,
    ) -> _SemanticEvidenceSelection:
        """Collect relevant sections and assess cumulative evidence coverage."""
        blocks: list[str] = []
        seen: set[str] = set()
        coverage = "unknown"
        missing = ""
        next_query = ""
        for tool in tools_used:
            if not self._is_concrete_read_tool_record(tool):
                continue
            result = str(tool.get("result") or "")
            selection = await self._select_relevant_markdown_evidence_semantic(
                user_request,
                result,
                session_key,
                max_sections=max_blocks,
                existing_evidence_blocks=[*(existing_evidence_blocks or []), *blocks],
            )
            if not selection.sections:
                continue
            coverage = selection.coverage
            missing = selection.missing
            next_query = selection.next_query
            for section in selection.sections:
                cleaned = self._prepare_text_block_for_rewrite(section)
                if not cleaned or cleaned in seen:
                    continue
                seen.add(cleaned)
                blocks.append(cleaned)
                if len(blocks) >= max_blocks:
                    return _SemanticEvidenceSelection(
                        sections=blocks,
                        coverage=coverage,
                        missing=missing,
                        next_query=next_query,
                    )
        return _SemanticEvidenceSelection(
            sections=blocks,
            coverage=coverage,
            missing=missing,
            next_query=next_query,
        )

    async def _collect_document_evidence_blocks_from_messages_semantic(
        self,
        user_request: str,
        messages: list[dict],
        session_key: SessionKey,
        *,
        max_blocks: int = 3,
    ) -> list[str]:
        """Collect relevant evidence blocks from message history with semantic selection."""
        tools_used: list[dict[str, Any]] = []
        for message_index, message in enumerate(messages):
            if message.get("role") != "tool" or message.get("name") != "openviking_read":
                continue
            tool_call_id = message.get("tool_call_id")
            args = self._find_tool_call_arguments(messages[:message_index], tool_call_id)
            tools_used.append(
                {
                    "tool_name": "openviking_read",
                    "args": json.dumps(args, ensure_ascii=False),
                    "result": message.get("content") or "",
                    "execute_success": True,
                }
            )
        return await self._collect_document_evidence_blocks_semantic(
            user_request,
            tools_used,
            session_key,
            max_blocks=max_blocks,
        )

    @classmethod
    def _collect_selected_evidence_blocks_from_prompts(
        cls, messages: list[dict], *, max_blocks: int = 3
    ) -> list[str]:
        """Reuse evidence blocks already selected during the current retrieval turn."""
        blocks: list[str] = []
        seen: set[str] = set()
        for message in messages:
            if message.get("role") != "system":
                continue
            content = message.get("content")
            if (
                not isinstance(content, str)
                or "Relevant document evidence for the current user request" not in content
            ):
                continue
            parts = re.split(r"(?m)^\[Evidence\s+\d+\]\s*$", content)
            for part in parts[1:]:
                cleaned = cls._prepare_text_block_for_rewrite(part)
                if not cleaned or cleaned in seen:
                    continue
                seen.add(cleaned)
                blocks.append(cleaned)
                if len(blocks) >= max_blocks:
                    return blocks
        return blocks

    @classmethod
    def _build_relevant_evidence_prompt(
        cls,
        user_request: str,
        evidence_blocks: list[str],
        *,
        source_uri: str = "",
    ) -> str:
        """Build a short system prompt that focuses the next answer on selected evidence."""
        numbered = "\n\n".join(
            f"[Evidence {index}]\n{block}" for index, block in enumerate(evidence_blocks, start=1)
        )
        source_uris = [
            re.sub(r"[\r\n]+", "", uri).strip()
            for uri in str(source_uri or "").splitlines()
            if uri.strip()
        ]
        source_line = (
            "\n" + "\n".join(f"Evidence source URI: {uri}" for uri in source_uris)
            if source_uris
            else ""
        )
        return (
            "Relevant document evidence for the current user request has been extracted below.\n"
            "Use only these evidence blocks for the final answer. If they do not answer the "
            "request, say the current documentation is insufficient instead of using unrelated "
            f"document text.{source_line}\n\n"
            f"User request:\n{user_request.strip()}\n\n"
            f"{numbered}"
        )

    @classmethod
    def _extract_selected_evidence_uris_from_prompts(cls, messages: list[dict]) -> list[str]:
        """Collect source URIs attached to semantically selected evidence prompts."""
        seen: set[str] = set()
        uris: list[str] = []
        for message in messages:
            if message.get("role") != "system":
                continue
            content = message.get("content")
            if not isinstance(content, str):
                continue
            for match in re.finditer(r"(?m)^Evidence source URI:\s*(viking://.*\S)\s*$", content):
                uri = match.group(1).strip()
                if uri and uri not in seen:
                    seen.add(uri)
                    uris.append(uri)
        return uris

    @classmethod
    def _prepare_text_block_for_rewrite(cls, text: str) -> str:
        """Clean imported Markdown noise while preserving answerable content."""
        cleaned_lines: list[str] = []
        for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
            line = raw_line.strip()
            if not line:
                cleaned_lines.append("")
                continue
            if cls.MARKDOWN_IMAGE_LINE_RE.fullmatch(line):
                cleaned_lines.append(line)
                continue
            line = line.replace("**", "")
            line = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
            line = re.sub(r"\s+", " ", line).strip()
            line = re.sub(r"^(\d+)\s*[）)]\s*", r"\1. ", line)
            line = re.sub(r"^(\d+(?:\.\d+)*)\s+", r"\1 ", line)
            line = re.sub(r"^(\d+(?:\.\d+)*)([\u4e00-\u9fffA-Za-z])", r"\1\2", line)
            cleaned_lines.append(line)

        compact_lines: list[str] = []
        blank_seen = False
        for line in cleaned_lines:
            if not line:
                if compact_lines and not blank_seen:
                    compact_lines.append("")
                blank_seen = True
                continue
            compact_lines.append(line)
            blank_seen = False
        while compact_lines and compact_lines[-1] == "":
            compact_lines.pop()
        return "\n".join(compact_lines)

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
        self,
        draft_content: str,
        image_segments: list[str],
        session_key: SessionKey,
        *,
        user_request: str = "",
        max_segments: int = 4,
    ) -> list[str]:
        """Ask the model to choose the minimum image segments needed for the answer."""
        if not image_segments or max_segments <= 0:
            return []

        numbered_segments = "\n\n".join(
            f"[Segment {index}]\n{segment}" for index, segment in enumerate(image_segments, start=1)
        )
        selection_messages = [
            {
                "role": "system",
                "content": (
                    "Decide whether screenshots or images materially improve the answer. "
                    "Select the minimum image evidence segments needed when they clarify UI "
                    "locations, visual states, or procedural steps. Do not select images for "
                    "simple definitions or when they add no useful information. "
                    f"Select at most {max_segments} segments. "
                    "Return only segment numbers separated by commas, or NONE. "
                    "Do not include any explanation."
                ),
            },
            {
                "role": "user",
                "content": (
                    "User request:\n"
                    f"{user_request}\n\n"
                    "Answer draft:\n"
                    f"{draft_content}\n\n"
                    "Candidate image evidence segments:\n"
                    f"{numbered_segments}"
                ),
            },
        ]
        try:
            selection = await self.provider.chat(
                messages=selection_messages,
                model=self.fast_model,
                max_tokens=64,
                temperature=0,
                session_id=f"{session_key.safe_name()}:kb-image-select",
            )
        except Exception as exc:
            logger.debug(f"[KB_TRACE] image selection unavailable; omitting images: {exc}")
            return []
        indexes = self._parse_selected_segment_indexes(selection.content or "", len(image_segments))
        return [image_segments[index - 1] for index in indexes[:max_segments]]

    async def _finalize_kb_response(
        self, draft_content: str, session_key: SessionKey, messages: list[dict]
    ) -> str:
        """Finalize a KB draft with references and optional image evidence.

        The image selector acts as the agent's decision point: if it selects
        evidence segments, include their nearby explanatory text and images in
        the same final reply without an extra image-aware rewrite.
        """
        if not self.context._is_retrieval_mode() or not draft_content:
            return draft_content

        finalize_start_time = time.time()
        trace_session = session_key.safe_name()
        current_turn_messages = messages[self._current_turn_start_index(messages) :]
        reference_links = self._build_reference_links(current_turn_messages)
        selected_evidence_blocks = self._collect_selected_evidence_blocks_from_prompts(
            current_turn_messages,
        )
        image_evidence_blocks = [
            block for block in selected_evidence_blocks if self.SEND_IMAGE_LINE_RE.search(block)
        ]
        if not selected_evidence_blocks:
            image_evidence_blocks = self._extract_image_evidence_blocks(current_turn_messages)
        image_evidence_segments = self._build_image_evidence_segments(image_evidence_blocks)
        image_select_start_time = time.time()
        selected_image_segments = await self._select_relevant_image_segments(
            draft_content,
            image_evidence_segments,
            session_key,
            user_request=self._extract_user_text(messages),
        )
        logger.info(
            f"[KB_TRACE] session={trace_session} finalize_image_selection "
            f"duration_ms={(time.time() - image_select_start_time) * 1000:.1f} "
            f"candidate_segments={len(image_evidence_segments)} "
            f"selected_segments={len(selected_image_segments)} "
            f"draft_has_images={bool(self.MARKDOWN_IMAGE_LINE_RE.search(str(draft_content or '')))} "
            f"model={self.fast_model}"
        )
        should_include_images = bool(selected_image_segments)

        if not should_include_images:
            result = self._append_reference_links(draft_content, reference_links)
            logger.info(
                f"[KB_TRACE] session={trace_session} finalize_fast_path "
                f"duration_ms={(time.time() - finalize_start_time) * 1000:.1f}"
            )
            return result

        image_content = self._build_inline_image_content(selected_image_segments)
        body_content = (
            f"{draft_content.rstrip()}\n\n{image_content}"
            if image_content.strip()
            else draft_content
        )
        final_content = self._append_reference_links(body_content, reference_links)
        logger.info(
            f"[KB_TRACE] session={trace_session} finalize_image_inline "
            f"duration_ms={(time.time() - finalize_start_time) * 1000:.1f} "
            f"selected_segments={len(selected_image_segments)} "
            f"image_lines={len(self._extract_send_image_lines_from_text(image_content))} "
            "image_inlined=True"
        )

        return final_content

    @classmethod
    def _build_inline_image_content(cls, selected_image_segments: list[str]) -> str:
        """Build a compact text-and-image block from selected screenshot evidence."""
        rendered_segments: list[str] = []
        seen_images: set[str] = set()
        for segment in selected_image_segments:
            segment_images = cls._extract_send_image_lines_from_text(segment)
            unique_images = [line for line in segment_images if line not in seen_images]
            if not unique_images:
                continue
            seen_images.update(unique_images)
            text_without_images = cls.SEND_IMAGE_LINE_RE.sub("", segment)
            text_without_images = re.sub(r"\n{3,}", "\n\n", text_without_images).strip()
            parts = [part for part in [text_without_images, "\n".join(unique_images)] if part]
            rendered_segments.append("\n\n".join(parts))
        if not rendered_segments:
            return ""
        return "相关图文说明：\n\n" + "\n\n".join(rendered_segments)

    def _build_reference_links(self, messages: list[dict]) -> list[str]:
        """Build Markdown links for concrete read document evidence used in retrieval answers."""
        if not self.context._is_retrieval_mode():
            return []

        read_uris = self._extract_selected_evidence_uris_from_prompts(messages)
        if not read_uris:
            read_uris = self._extract_read_evidence_uris(messages)
        links: list[str] = []
        preview_secret = (
            os.environ.get(RESOURCE_PREVIEW_SECRET_ENV, "").strip()
            or str(self.config.ov_server.root_api_key or "").strip()
        )
        if not preview_secret:
            logger.warning(
                "Resource preview links disabled because no preview secret is configured"
            )
            return []
        for uri in read_uris[:3]:
            title = self._reference_title_from_uri(uri)
            try:
                token = create_resource_preview_token(
                    uri=uri,
                    account_id=self.config.ov_server.account_id,
                    secret=preview_secret,
                )
            except ResourcePreviewTokenError as exc:
                logger.warning(f"Failed to sign resource preview URI {uri}: {exc}")
                continue
            href = (
                f"/bot/v1/resources/preview?uri={quote(uri, safe='')}&token={quote(token, safe='')}"
            )
            links.append(f"- [{title}]({href})")
        return links

    @staticmethod
    def _extract_read_evidence_uris(messages: list[dict]) -> list[str]:
        """Collect concrete openviking_read URIs from tool-result messages."""
        seen: set[str] = set()
        uris: list[str] = []

        for message_index, message in enumerate(messages):
            if message.get("role") != "tool" or message.get("name") != "openviking_read":
                continue
            content = message.get("content")
            if (
                not isinstance(content, str)
                or not content.strip()
                or AgentLoop._classify_tool_error_result(content)
            ):
                continue

            tool_call_id = message.get("tool_call_id")
            if not tool_call_id:
                continue

            args = AgentLoop._find_tool_call_arguments(messages[:message_index], tool_call_id)
            uri = str(args.get("uri") or "").strip()
            level = str(args.get("level", "abstract") or "abstract")
            if (
                not uri
                or level != "read"
                or is_summary_uri(uri)
                or is_generic_scope_summary_uri(uri)
                or uri in seen
            ):
                continue
            seen.add(uri)
            uris.append(uri)

        return uris

    @staticmethod
    def _find_tool_call_arguments(messages: list[dict], tool_call_id: str) -> dict[str, Any]:
        """Find parsed function-call arguments by tool-call id."""
        for prior in reversed(messages):
            if prior.get("role") != "assistant":
                continue
            tool_calls = prior.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict) or tool_call.get("id") != tool_call_id:
                    continue
                fn = tool_call.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    return {}
                return args if isinstance(args, dict) else {}
        return {}

    @staticmethod
    def _reference_title_from_uri(uri: str) -> str:
        """Create a compact user-facing label from a Viking URI."""
        normalized = uri.rstrip("/")
        path = (
            normalized.split("viking://resources/", 1)[1]
            if normalized.startswith("viking://resources/")
            else normalized
        )
        parts = [unquote(part).strip() for part in path.split("/") if part.strip()]
        name = parts[-1] if parts else uri
        if "." in name:
            name = name.rsplit(".", 1)[0]
        name = re.sub(r"[_-]+", " ", name).strip()
        parent = parts[-2] if len(parts) > 1 else ""
        if parent and name:
            return f"{parent} / {name}"
        return name or normalized or "参考文档"

    @staticmethod
    def _append_reference_links(content: str, links: list[str]) -> str:
        """Append a reference section once, preserving the answer body."""
        if not links:
            return content
        if re.search(r"(?m)^#{0,6}\s*参考文档\s*$", content or ""):
            return content
        return f"{content.rstrip()}\n\n参考文档\n" + "\n".join(links)

    async def _persist_session_turn(
        self,
        session,
        msg: InboundMessage,
        assistant_content: str,
        *,
        tools_used: list[dict[str, Any]] | None = None,
        token_usage: dict[str, Any] | None = None,
    ) -> None:
        """Persist one completed user/assistant turn locally and to OpenViking when needed."""
        memory_scope = ""
        if isinstance(session.metadata, dict):
            raw_memory_scope = session.metadata.get("openviking_memory_scope")
            if isinstance(raw_memory_scope, str):
                memory_scope = raw_memory_scope.strip().lower()

        openviking_session_id = ""
        if msg.metadata:
            raw_session_id = msg.metadata.get("openviking_session_id")
            if isinstance(raw_session_id, str):
                openviking_session_id = raw_session_id.strip()

        message_kwargs: dict[str, Any] = {}
        if openviking_session_id:
            if memory_scope and memory_scope != "all":
                raise ValueError(
                    "OpenViking live session mirroring only supports direct user-scoped sessions"
                )
            session.metadata["openviking_session_id"] = openviking_session_id
            message_kwargs["openviking_session_id"] = openviking_session_id

        session.add_message("user", msg.content, sender_id=msg.sender_id, **message_kwargs)

        assistant_kwargs = dict(message_kwargs)
        if tools_used:
            assistant_kwargs["tools_used"] = tools_used
        if token_usage is not None:
            assistant_kwargs["token_usage"] = token_usage
        session.add_message(
            "assistant", assistant_content, sender_id=msg.sender_id, **assistant_kwargs
        )

        await self.sessions.save(session)
        if openviking_session_id and memory_scope == "all":
            self._schedule_openviking_sync(msg.session_key)

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
            self._apply_openviking_memory_policy(session, msg)

            # Handle slash commands
            is_group_chat = self._metadata_indicates_shared_session(msg.metadata)
            if is_group_chat:
                cmd = msg.content.replace(f"@{msg.sender_id}", "").strip().lower()
            else:
                cmd = msg.content.strip().lower()
            if cmd == "/new":
                # Clone session for async consolidation, then immediately clear original
                if not self._check_cmd_auth(msg):
                    return OutboundMessage(
                        session_key=msg.session_key,
                        content="🐈 Sorry, you are not authorized to use this command.",
                        metadata=msg.metadata,
                    )
                session_clone = session.clone()
                session.clear()
                await self.sessions.save(session)
                # Run consolidation in background
                await self._safe_consolidate_memory(session_clone, archive_all=True)
                return OutboundMessage(
                    session_key=msg.session_key,
                    content="🐈 New session started. Memory consolidated.",
                    metadata=msg.metadata,
                )
            if cmd == "/remember":
                if not self._check_cmd_auth(msg):
                    return OutboundMessage(
                        session_key=msg.session_key,
                        content="🐈 Sorry, you are not authorized to use this command.",
                        metadata=msg.metadata,
                    )
                session_clone = session.clone()
                await self._consolidate_viking_memory(session_clone)
                return OutboundMessage(
                    session_key=msg.session_key,
                    content="This conversation has been submitted to memory storage.",
                    metadata=msg.metadata,
                )
            if cmd == "/help":
                return OutboundMessage(
                    session_key=msg.session_key,
                    content="🐈 vikingbot commands:\n/new — Start a new conversation\n/remember — Submit current session to memories and start new session\n/help — Show available commands",
                    metadata=msg.metadata,
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

            # Retrieval-focused profiles: classify intent before agent loop
            if message_context._is_retrieval_mode():
                try:
                    logger.info("[IntentRouter] Classifying user intent...")
                    decision = await classify_intent(
                        provider=self.provider,
                        model=self.fast_model,
                        user_message=msg.content,
                        history=session.get_history(),
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
                            history=session.get_history(),
                            session_id=session_key.safe_name(),
                        )
                        response_text = self._normalize_final_output_text(response_text)
                        await self._persist_session_turn(session, msg, response_text)

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
            allow_grounded_history_reuse = self._latest_assistant_reply_has_document_evidence(
                session
            )
            if allow_grounded_history_reuse:
                messages.append(
                    {
                        "role": "system",
                        "content": self._build_grounded_history_reuse_prompt(),
                    }
                )
            # logger.info(f"New messages: {messages}")

            # Run agent loop
            final_content, tools_used, token_usage, iteration = await self._run_agent_loop(
                messages=messages,
                session_key=session_key,
                publish_events=True,
                sender_id=msg.sender_id,
                allow_grounded_history_reuse=allow_grounded_history_reuse,
            )

            # Log response preview
            preview = final_content[:300] + "..." if len(final_content) > 300 else final_content
            logger.info(f"Response to {msg.session_key}: {preview}")

            # Save to session (include tool names so consolidation sees what happened)
            await self._persist_session_turn(
                session,
                msg,
                final_content,
                tools_used=tools_used if tools_used else None,
                token_usage=token_usage,
            )

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
                tools_used_names=tools_used_names,
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

        if final_content is None or (isinstance(final_content, str) and not final_content.strip()):
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
                logger.info(
                    f"No messages to commit openviking for session {session.key.safe_name()} (allow_from filter applied)"
                )
                return

            # use openviking tools to extract memory
            await hook_manager.execute_hooks(
                context=HookContext(
                    event_type="message.compact",
                    session_id=session.key.safe_name(),
                    workspace_id=self.sandbox_manager.to_workspace_id(session.key),
                    session_key=session.key,
                    metadata=dict(session.metadata),
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
                allow_cmd = getattr(channel, "allow_cmd_from", [])
                if allow_cmd:
                    allow_from.extend(allow_cmd)
                break

        # If channel not found or sender not in allow_from list, ignore message
        if msg.sender_id not in allow_from:
            logger.debug(
                f"Sender {msg.sender_id} not allowed in channel {msg.session_key.channel_key()}"
            )
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
