"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from vikingbot.agent.context import ContextBuilder
from vikingbot.agent.guided_questions import (
    build_guided_questions,
    should_attempt_guided_questions,
    verify_guided_question_token,
)
from vikingbot.agent.intent_router import (
    IntentRoute,
    classify_intent,
    detect_reply_language,
    generate_route_response,
)
from vikingbot.agent.kb_evidence import KbEvidenceMixin
from vikingbot.agent.kb_response import KbResponseMixin
from vikingbot.agent.loop_trace import LoopTraceMixin
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
from vikingbot.utils.markdown_images import repair_send_image_markdown
from vikingbot.utils.tracing import trace

if TYPE_CHECKING:
    from vikingbot.config.schema import ExecToolConfig
    from vikingbot.cron.service import CronService


@dataclass
class _FastBatchSearchPlan:
    """Deterministic KB retrieval plan derived from one focused search result."""

    document_uris: list[str]
    total_matches: int | None = None


class AgentLoop(LoopTraceMixin, KbEvidenceMixin, KbResponseMixin):
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

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
        self._streamed_response_text: dict[str, str] = {}
        self._guided_question_token_secret = secrets.token_urlsafe(32)
        self._register_default_tools()

    def _should_use_kb_fast_batch_path(self) -> bool:
        """Use deterministic batch retrieval for KB answers when search/read tools exist."""
        if not self.context._is_knowledge_base_mode():
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
            if not uri or uri in seen or is_summary_uri(uri) or is_generic_scope_summary_uri(uri):
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

    @classmethod
    def _extract_memory_hint_text_for_retrieval(
        cls,
        memory_hints: str,
        *,
        max_chars: int = 500,
    ) -> str:
        """Extract compact memory hint text suitable for a document search query."""
        if not memory_hints:
            return ""

        hint_parts: list[str] = []
        for tag in ["abstract", "overview", "match_reason"]:
            for match in re.finditer(
                rf"<{tag}>(.*?)</{tag}>",
                memory_hints,
                flags=re.IGNORECASE | re.DOTALL,
            ):
                text = re.sub(r"\s+", " ", match.group(1)).strip()
                if text and text not in hint_parts:
                    hint_parts.append(text)

        if not hint_parts:
            cleaned = re.sub(r"<[^>]+>", " ", memory_hints)
            cleaned = re.sub(r"\s+", " ", cleaned).strip()
            hint_parts = [cleaned] if cleaned else []

        return "；".join(hint_parts)[:max_chars].strip()

    @classmethod
    def _build_memory_guided_retrieval_query(
        cls,
        user_request: str,
        memory_hints: str,
    ) -> str:
        """Use agent memory as retrieval guidance without treating it as answer evidence."""
        request = str(user_request or "").strip()
        hint_text = cls._extract_memory_hint_text_for_retrieval(memory_hints)
        if not request or not hint_text:
            return request
        return (
            f"{request}\n\n"
            "检索提示（来自历史有用回答的记忆，仅用于选择关键词和文档范围，不作为答案事实）："
            f"{hint_text}"
        )

    @staticmethod
    def _extract_agent_memory_hints_from_messages(messages: list[dict]) -> str:
        """Return existing agent-memory guidance already attached to the turn."""
        hints = [
            str(message.get("content") or "").strip()
            for message in messages
            if message.get("role") == "system"
            and "Agent memory hints from helpful feedback" in str(message.get("content") or "")
        ]
        return "\n\n".join(hint for hint in hints if hint)

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

    async def _publish_response_delta_events(
        self,
        *,
        session_key: SessionKey,
        content: str | None,
        publish_events: bool,
    ) -> None:
        """Publish final answer chunks for clients that render streaming text."""
        if not publish_events or not content:
            return

        chunks = self._split_response_delta_chunks(content)
        for chunk in chunks:
            await self.bus.publish_outbound(
                OutboundMessage(
                    session_key=session_key,
                    content=chunk,
                    event_type=OutboundEventType.RESPONSE_DELTA,
                )
            )

    async def _publish_missing_response_delta_events(
        self,
        *,
        session_key: SessionKey,
        final_content: str | None,
        publish_events: bool,
    ) -> None:
        """Publish only response text that was not already emitted as provider deltas."""
        if not publish_events or not final_content:
            self._consume_streamed_response_text(session_key)
            return

        streamed_content = self._consume_streamed_response_text(session_key)
        if not streamed_content:
            await self._publish_response_delta_events(
                session_key=session_key,
                content=final_content,
                publish_events=publish_events,
            )
            return

        if final_content.startswith(streamed_content):
            await self._publish_response_delta_events(
                session_key=session_key,
                content=final_content[len(streamed_content) :],
                publish_events=publish_events,
            )

    @staticmethod
    def _split_response_delta_chunks(content: str, max_chars: int = 24) -> list[str]:
        """Split text into readable UI chunks without changing its content."""
        if not content:
            return []

        chunks: list[str] = []
        current = ""
        for char in content:
            current += char
            if char in "\n。！？!?；;，,、 " or len(current) >= max_chars:
                chunks.append(current)
                current = ""
        if current:
            chunks.append(current)
        return chunks

    def _make_response_delta_callback(
        self,
        *,
        session_key: SessionKey,
        publish_events: bool,
    ) -> Callable[[str], Awaitable[None]] | None:
        """Build a provider streaming callback for final response text."""
        if not publish_events:
            return None

        async def publish_delta(delta: str) -> None:
            if not delta:
                return
            safe_name = session_key.safe_name()
            self._streamed_response_text[safe_name] = (
                self._streamed_response_text.get(safe_name, "") + delta
            )
            await self.bus.publish_outbound(
                OutboundMessage(
                    session_key=session_key,
                    content=delta,
                    event_type=OutboundEventType.RESPONSE_DELTA,
                )
            )

        return publish_delta

    def _consume_streamed_response_text(self, session_key: SessionKey) -> str:
        """Return response text already emitted as deltas and clear the marker."""
        return self._streamed_response_text.pop(session_key.safe_name(), "")

    def _is_verified_guided_question_request(self, msg: InboundMessage) -> bool:
        """Validate a UI suggestion token before letting it bypass intent routing."""
        metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        token = metadata.get("guided_question_token")
        if not isinstance(token, str) or not token:
            return False
        return verify_guided_question_token(
            token=token,
            session_id=msg.session_key.safe_name(),
            canonical_question=msg.content,
            secret=self._guided_question_token_secret,
        )

    @staticmethod
    def _guided_question_response_text(user_message: str) -> str:
        reply_language = detect_reply_language(user_message)
        if reply_language == "zh-CN":
            return "你可能想问这些，选一个我继续查："
        if reply_language == "ja":
            return "聞きたい内容に近いものを選んでください。続けて調べます："
        return "Pick the closest question and I will keep looking:"

    @staticmethod
    def _build_persisted_user_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
        """Persist durable guided-question selection metadata without storing tokens."""
        if not isinstance(metadata, dict):
            return {}

        result: dict[str, Any] = {}
        guided_question_id = metadata.get("guided_question_id")
        if isinstance(guided_question_id, str) and guided_question_id.strip():
            result["guided_question_id"] = guided_question_id.strip()

        source_uris = metadata.get("guided_source_uris")
        if isinstance(source_uris, list):
            normalized_source_uris = [
                str(uri).strip()
                for uri in source_uris
                if str(uri).strip()
            ]
            if normalized_source_uris:
                result["guided_source_uris"] = normalized_source_uris[:10]

        return result

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
                    self._consume_streamed_response_text(msg.session_key)
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
        from vikingbot.openviking_identity import resolve_agent_memory_identity

        return resolve_agent_memory_identity(self.config).owner_user_id

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
        stream_response_events: bool = False,
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
        if self.context._is_knowledge_base_mode():
            if self._should_use_kb_fast_batch_path():
                return await self._run_kb_fast_batch_loop(
                    messages=messages,
                    session_key=session_key,
                    publish_events=publish_events,
                    sender_id=sender_id,
                    allow_grounded_history_reuse=allow_grounded_history_reuse,
                    stream_response_events=stream_response_events,
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
            return (
                final_content,
                [],
                {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                0,
            )

        return await self._run_agent_loop_classic(
            messages=messages,
            session_key=session_key,
            publish_events=publish_events,
            sender_id=sender_id,
            allow_grounded_history_reuse=allow_grounded_history_reuse,
            stream_response_events=stream_response_events,
        )

    async def _run_kb_fast_batch_loop(
        self,
        messages: list[dict],
        session_key: SessionKey,
        publish_events: bool = True,
        sender_id: str | None = None,
        allow_grounded_history_reuse: bool = False,
        stream_response_events: bool = False,
    ) -> tuple[str | None, list[dict], dict[str, int], int]:
        """Run the deterministic no-fallback KB path: search, batch read, answer."""
        trace_session = session_key.safe_name()
        token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        tools_used: list[dict] = []
        user_request = self._extract_user_text(messages)
        retrieval_query = user_request.strip()
        agent_memory_hints = self._extract_agent_memory_hints_from_messages(messages)
        agent_memory_stats = self.context.agent_memory_read_stats

        if not agent_memory_hints:
            if agent_memory_stats.get("status") == "not_started":
                try:
                    agent_memory_hints = await self.context._build_knowledge_base_agent_memory(
                        session_key,
                        user_request,
                    )
                except Exception as exc:
                    logger.debug(
                        f"[KB_TRACE] session={trace_session} retrieval_path=fast_batch "
                        f"stage=memory status=unavailable reason={exc}"
                    )
                if agent_memory_hints:
                    messages.append({"role": "system", "content": agent_memory_hints})
                agent_memory_stats = self.context.agent_memory_read_stats
        else:
            if agent_memory_stats.get("status") == "not_started":
                self.context.mark_agent_memory_hints_reused()
            agent_memory_stats = self.context.agent_memory_read_stats
        if agent_memory_hints:
            retrieval_query = self._build_memory_guided_retrieval_query(
                user_request,
                agent_memory_hints,
            )

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
            f"memory_hints={'yes' if agent_memory_hints else 'no'} "
            f"memory_read_status={agent_memory_stats.get('status', 'unknown')} "
            f"memory_read_cost_ms={agent_memory_stats.get('duration_ms', 0)} "
            f"memory_read_timeout_ms={agent_memory_stats.get('timeout_ms', 0)} "
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
            memory_hints=agent_memory_hints,
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
        final_content = await self._compose_answer_from_selected_evidence(
            messages,
            session_key,
            publish_events=stream_response_events,
        )
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
        stream_response_events: bool = False,
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
        trace_enabled = self.context._is_knowledge_base_mode()
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
                and self.context._is_knowledge_base_mode()
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
                if self.context._is_knowledge_base_mode() and not has_sufficient_kb_evidence:
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
            if self.context._is_knowledge_base_mode() and has_kb_read_evidence:
                if trace_enabled:
                    logger.info(
                        f"[KB_TRACE] session={trace_session} empty_final_with_selected_evidence "
                        "action=answer_from_selected_evidence"
                    )
                final_content = await self._compose_answer_from_selected_evidence(
                    messages,
                    session_key,
                    publish_events=stream_response_events,
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
        if not self.context._is_knowledge_base_mode():
            return False, "not_knowledge_base_mode"

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
        return detect_reply_language(user_text)

    def _build_iteration_limit_terminal_response(
        self, messages: list[dict], has_kb_read_evidence: bool
    ) -> str:
        """Return the terminal response when retrieval hits the iteration limit."""
        if not self.context._is_knowledge_base_mode():
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
        publish_events: bool = False,
    ) -> str | None:
        """Answer from already selected evidence without running more retrieval tools."""
        current_turn_messages = messages[self._current_turn_start_index(messages) :]
        evidence_blocks = self._collect_selected_evidence_blocks_from_prompts(
            current_turn_messages,
        )
        if not evidence_blocks:
            return None
        agent_memory_hints = self._extract_agent_memory_hints_from_messages(current_turn_messages)

        final_messages = [
            {
                "role": "system",
                "content": self.context.build_retrieval_final_response_system_prompt(),
            },
            *([{"role": "system", "content": agent_memory_hints}] if agent_memory_hints else []),
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
            chat_kwargs: dict[str, Any] = {}
            on_delta = self._make_response_delta_callback(
                session_key=session_key,
                publish_events=publish_events,
            )
            if on_delta is not None:
                chat_kwargs["on_delta"] = on_delta
            response = await self.provider.chat(
                messages=final_messages,
                model=self.model,
                session_id=f"{session_key.safe_name()}:kb-selected-evidence-answer",
                **chat_kwargs,
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
        if self.context._is_knowledge_base_mode() and not has_sufficient_kb_evidence and tools:
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

        normalized = (
            (repair_send_image_markdown(content) or content)
            .replace("\r\n", "\n")
            .replace("\r", "\n")
        )
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

    async def _persist_session_turn(
        self,
        session,
        msg: InboundMessage,
        assistant_content: str,
        *,
        tools_used: list[dict[str, Any]] | None = None,
        token_usage: dict[str, Any] | None = None,
        assistant_metadata: dict[str, Any] | None = None,
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

        user_kwargs = dict(message_kwargs)
        user_metadata = self._build_persisted_user_metadata(msg.metadata)
        if user_metadata:
            user_kwargs["metadata"] = user_metadata
        session.add_message("user", msg.content, sender_id=msg.sender_id, **user_kwargs)

        assistant_kwargs = dict(message_kwargs)
        if tools_used:
            assistant_kwargs["tools_used"] = tools_used
        if token_usage is not None:
            assistant_kwargs["token_usage"] = token_usage
        if assistant_metadata:
            assistant_kwargs["metadata"] = assistant_metadata
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
        self._consume_streamed_response_text(msg.session_key)

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

            should_stream_response = bool(msg.metadata and "openviking_session_id" in msg.metadata)
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
                if self._is_verified_guided_question_request(msg):
                    logger.info("[GuidedQuestions] verified suggestion token; skipping intent router")
                else:
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
                            if (
                                should_attempt_guided_questions(msg.content, decision)
                                and self._has_kb_fast_batch_tools()
                            ):
                                guided_questions = []

                                async def search_for_guidance(query: str, limit: int) -> str:
                                    result, _duration_ms = await self._execute_fast_batch_tool(
                                        tool_name="openviking_search",
                                        arguments={
                                            "query": query,
                                            "target_uri": "viking://resources/",
                                            "limit": limit,
                                        },
                                        session_key=msg.session_key,
                                        sender_id=msg.sender_id,
                                    )
                                    return result

                                try:
                                    guided_questions = await build_guided_questions(
                                        provider=self.provider,
                                        generation_model=self.model,
                                        classifier_model=self.fast_model,
                                        user_message=msg.content,
                                        history=session.get_history(),
                                        session_id=session_key.safe_name(),
                                        search=search_for_guidance,
                                        token_secret=self._guided_question_token_secret,
                                    )
                                except Exception as guided_exc:
                                    logger.info(
                                        f"[GuidedQuestions] suggestion generation skipped: {guided_exc}",
                                        exc_info=True,
                                    )

                                if guided_questions:
                                    response_text = self._guided_question_response_text(msg.content)
                                    guided_question_items = [
                                        question.to_dict() for question in guided_questions
                                    ]
                                    await self._persist_session_turn(
                                        session,
                                        msg,
                                        response_text,
                                        assistant_metadata={"guided_questions": guided_question_items},
                                    )
                                    await self._publish_missing_response_delta_events(
                                        session_key=msg.session_key,
                                        final_content=response_text,
                                        publish_events=should_stream_response,
                                    )
                                    response_metadata = dict(msg.metadata or {})
                                    response_metadata["guided_questions"] = guided_question_items

                                    time_cost = round(time.time() - start_time, 2)
                                    return OutboundMessage(
                                        session_key=msg.session_key,
                                        content=response_text,
                                        metadata=response_metadata,
                                        time_cost=time_cost,
                                    )

                            # Non-retrieval route: generate a direct response
                            response_text = await generate_route_response(
                                provider=self.provider,
                                model=self.model,
                                route_label=decision.label,
                                user_message=msg.content,
                                history=session.get_history(),
                                session_id=session_key.safe_name(),
                                on_delta=self._make_response_delta_callback(
                                    session_key=msg.session_key,
                                    publish_events=should_stream_response,
                                ),
                            )
                            response_text = self._normalize_final_output_text(response_text)
                            await self._persist_session_turn(session, msg, response_text)
                            await self._publish_missing_response_delta_events(
                                session_key=msg.session_key,
                                final_content=response_text,
                                publish_events=should_stream_response,
                            )

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
                        self._consume_streamed_response_text(msg.session_key)

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
                stream_response_events=should_stream_response,
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
            await self._publish_missing_response_delta_events(
                session_key=msg.session_key,
                final_content=final_content,
                publish_events=should_stream_response,
            )
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
