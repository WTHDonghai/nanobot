"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
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
from vikingbot.openviking_mount.uri_utils import is_generic_scope_summary_uri, is_summary_uri
from vikingbot.sandbox import SandboxManager
from vikingbot.session.manager import SessionManager
from vikingbot.utils.helpers import cal_str_tokens
from vikingbot.utils.tracing import trace

if TYPE_CHECKING:
    from vikingbot.config.schema import ExecToolConfig
    from vikingbot.cron.service import CronService

MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")


@dataclass
class KBSearchProgress:
    searched_queries: list[str]
    globbed_file_uris: list[str]
    generic_summary_reads: list[str]
    concrete_read_uris: list[str]
    relevant_evidence_blocks: list[str]

    @property
    def has_concrete_read(self) -> bool:
        return bool(self.concrete_read_uris)

    @property
    def has_relevant_evidence(self) -> bool:
        return bool(self.relevant_evidence_blocks)

    @property
    def answer_ready(self) -> bool:
        return self.has_concrete_read and self.has_relevant_evidence


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

    @classmethod
    def _has_document_evidence(cls, tools_used: list[dict]) -> bool:
        """Whether tool results contain document-backed evidence for answering."""
        evidence_tools = {"openviking_read", "openviking_grep"}
        for tool_used in tools_used:
            tool_name = tool_used.get("tool_name")
            if tool_name not in evidence_tools:
                continue
            if not tool_used.get("execute_success"):
                continue
            result = tool_used.get("result")
            if not isinstance(result, str) or not result.strip():
                continue

            if tool_name == "openviking_read":
                args = cls._parse_tool_args(tool_used)
                uri = str(args.get("uri") or args.get("target_uri") or "")
                if is_generic_scope_summary_uri(uri):
                    continue

            return True
        return False

    @staticmethod
    def _normalize_kb_prefetch_query(user_request: str | None) -> str:
        """Prepare the original user question for deterministic KB prefetch."""
        if not isinstance(user_request, str):
            return ""

        normalized = user_request.strip()
        normalized = re.sub(r"^[\s，,。.!！？?：:;；]+|[\s，,。.!！？?：:;；]+$", "", normalized)
        return normalized or user_request.strip()

    @staticmethod
    def _extract_search_result_uris(search_result: str) -> list[str]:
        """Extract candidate document URIs from formatted openviking_search output."""
        if not isinstance(search_result, str):
            return []

        preferred: list[str] = []
        summary_candidates: list[str] = []
        seen: set[str] = set()
        pattern = re.compile(r"(?m)^\d+\.\s+\[(?P<resource_type>[^\]]+)\]\s+(?P<uri>\S+)\s*$")
        for match in pattern.finditer(search_result):
            resource_type = match.group("resource_type").strip().lower()
            uri = match.group("uri").strip()
            if not uri or uri in seen or resource_type == "image asset":
                continue
            seen.add(uri)
            if is_generic_scope_summary_uri(uri):
                continue
            if is_summary_uri(uri):
                summary_candidates.append(uri)
            else:
                preferred.append(uri)

        return preferred + summary_candidates

    @staticmethod
    def _extract_glob_result_uris(glob_result: str) -> list[str]:
        """Extract concrete file URIs from formatted openviking_glob output."""
        if not isinstance(glob_result, str):
            return []
        uris: list[str] = []
        seen: set[str] = set()
        for match in re.finditer(r"(?m)^📄\s+(?P<uri>\S+)\s*$", glob_result):
            uri = match.group("uri").strip()
            if not uri or uri in seen:
                continue
            seen.add(uri)
            uris.append(uri)
        return uris

    async def _execute_prefetched_tool(
        self,
        tool_name: str,
        tool_args: dict[str, object],
        session_key: SessionKey,
        sender_id: str | None,
        publish_events: bool,
    ) -> dict:
        """Execute a deterministic KB prefetch tool call and record it like normal tool usage."""
        started_at = time.time()
        result = await self.tools.execute(
            tool_name,
            tool_args,
            session_key=session_key,
            sandbox_manager=self.sandbox_manager,
            sender_id=sender_id,
        )
        duration = (time.time() - started_at) * 1000
        args_str = json.dumps(tool_args, ensure_ascii=False)
        logger.info(f"[KB_PREFETCH_TOOL]: {tool_name}({args_str[:200]})")
        logger.info(f"[KB_PREFETCH_RESULT]: {str(result)[:600]}")

        if publish_events:
            await self.bus.publish_outbound(
                OutboundMessage(
                    session_key=session_key,
                    content=f"{tool_name}({args_str})",
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

        return {
            "tool_name": tool_name,
            "args": args_str,
            "result": result,
            "duration": duration,
            "execute_success": True if result and "Error executing" not in result else False,
            "input_token": cal_str_tokens(args_str, text_type="mixed"),
            "output_token": cal_str_tokens(result, text_type="mixed"),
        }

    async def _prefetch_knowledge_base_evidence(
        self,
        messages: list[dict],
        session_key: SessionKey,
        sender_id: str | None,
        user_request: str | None,
        publish_events: bool,
    ) -> tuple[list[dict], list[dict]]:
        """Run deterministic retrieval before the first KB model turn."""
        if not self._is_knowledge_base_mode():
            return messages, []

        query = self._normalize_kb_prefetch_query(user_request)
        if not query:
            return messages, []

        prefetched_tools: list[dict] = []
        search_tool = await self._execute_prefetched_tool(
            tool_name="openviking_search",
            tool_args={"query": query, "target_uri": "viking://resources/"},
            session_key=session_key,
            sender_id=sender_id,
            publish_events=publish_events,
        )
        prefetched_tools.append(search_tool)

        candidate_uris = self._extract_search_result_uris(str(search_tool["result"]))
        for uri in candidate_uris[:3]:
            read_tool = await self._execute_prefetched_tool(
                tool_name="openviking_read",
                tool_args={"uri": uri, "level": "read"},
                session_key=session_key,
                sender_id=sender_id,
                publish_events=publish_events,
            )
            prefetched_tools.append(read_tool)

        tool_call_dicts = [
            {
                "id": f"kb_prefetch_call_{index}",
                "type": "function",
                "function": {
                    "name": tool_used["tool_name"],
                    "arguments": tool_used["args"],
                },
            }
            for index, tool_used in enumerate(prefetched_tools, start=1)
        ]
        messages = self.context.add_assistant_message(
            messages,
            content=None,
            tool_calls=tool_call_dicts,
        )
        for tool_call, tool_used in zip(tool_call_dicts, prefetched_tools):
            messages = self.context.add_tool_result(
                messages,
                tool_call["id"],
                tool_used["tool_name"],
                str(tool_used["result"]),
            )

        return messages, prefetched_tools

    @staticmethod
    def _parse_tool_args(tool_used: dict) -> dict:
        """Parse serialized tool args from the session trace."""
        raw_args = tool_used.get("args")
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _split_numbered_sections(content: str) -> list[dict[str, str | int]]:
        """Split plain text by numbered section headings like 2.1宾客状态."""
        heading_re = re.compile(r"(?m)^(?P<title>\d+(?:\.\d+)+\s*[^\n]{1,80})\s*$")
        matches = list(heading_re.finditer(content))
        if not matches:
            return []

        sections: list[dict[str, str | int]] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            section_text = content[match.start() : end].strip()
            if not section_text:
                continue
            sections.append(
                {
                    "title": match.group("title").strip(),
                    "level": 1,
                    "content": section_text,
                }
            )
        return sections

    @classmethod
    def _split_evidence_sections(cls, content: str) -> list[dict[str, str | int]]:
        """Split evidence content into semantic sections when possible."""
        markdown_sections = cls._split_markdown_sections(content)
        if markdown_sections:
            return markdown_sections
        return cls._split_numbered_sections(content)

    @classmethod
    def _score_tool_evidence(cls, user_request: str | None, tool_used: dict) -> tuple[int, str]:
        """Score one tool result as documentation evidence."""
        result = tool_used.get("result")
        if not isinstance(result, str) or not result.strip():
            return 0, ""

        args = cls._parse_tool_args(tool_used)
        uri = str(args.get("uri") or args.get("target_uri") or "")
        sections = cls._split_evidence_sections(result)
        if sections:
            scored_sections = [
                (
                    cls._score_evidence_block(
                        user_request,
                        str(section["title"]),
                        str(section["content"]),
                        uri,
                    ),
                    cls._truncate_evidence_block(str(section["content"])),
                )
                for section in sections
            ]
            return max(scored_sections, key=lambda item: item[0], default=(0, ""))

        excerpt = cls._truncate_evidence_block(result)
        return cls._score_evidence_block(user_request, "", excerpt, uri), excerpt

    @classmethod
    def _analyze_kb_search_progress(
        cls, user_request: str | None, tools_used: list[dict]
    ) -> KBSearchProgress:
        """Summarize current KB search progress for continue/stop decisions."""
        searched_queries: list[str] = []
        globbed_file_uris: list[str] = []
        generic_summary_reads: list[str] = []
        concrete_read_uris: list[str] = []

        for tool_used in tools_used:
            if not tool_used.get("execute_success"):
                continue

            tool_name = tool_used.get("tool_name")
            args = cls._parse_tool_args(tool_used)
            result = tool_used.get("result")

            if tool_name == "openviking_search":
                query = str(args.get("query") or "").strip()
                if query and query not in searched_queries:
                    searched_queries.append(query)
            elif tool_name == "openviking_glob":
                for uri in cls._extract_glob_result_uris(str(result)):
                    if uri not in globbed_file_uris:
                        globbed_file_uris.append(uri)
            elif tool_name == "openviking_read":
                uri = str(args.get("uri") or "").strip()
                if not uri:
                    continue
                if is_generic_scope_summary_uri(uri):
                    if uri not in generic_summary_reads:
                        generic_summary_reads.append(uri)
                else:
                    if uri not in concrete_read_uris:
                        concrete_read_uris.append(uri)

        relevant_evidence_blocks = cls._collect_document_evidence_blocks(user_request, tools_used, limit=2)
        if relevant_evidence_blocks:
            concrete_relevant_found = False
            for tool_used in tools_used:
                if tool_used.get("tool_name") != "openviking_read" or not tool_used.get("execute_success"):
                    continue
                args = cls._parse_tool_args(tool_used)
                uri = str(args.get("uri") or "").strip()
                if not uri or is_generic_scope_summary_uri(uri):
                    continue
                score, _excerpt = cls._score_tool_evidence(user_request, tool_used)
                if score > 0:
                    concrete_relevant_found = True
                    break
            if not concrete_relevant_found:
                relevant_evidence_blocks = []

        return KBSearchProgress(
            searched_queries=searched_queries,
            globbed_file_uris=globbed_file_uris,
            generic_summary_reads=generic_summary_reads,
            concrete_read_uris=concrete_read_uris,
            relevant_evidence_blocks=relevant_evidence_blocks,
        )

    @staticmethod
    def _format_kb_search_progress(progress: KBSearchProgress) -> str:
        """Render KB search progress into a concise prompt summary."""
        lines: list[str] = []
        if progress.searched_queries:
            lines.append("Queries tried:")
            lines.extend(f"- {query}" for query in progress.searched_queries[:5])
        if progress.generic_summary_reads:
            lines.append("Generic scope summaries already read (non-evidence):")
            lines.extend(f"- {uri}" for uri in progress.generic_summary_reads[:3])
        if progress.globbed_file_uris:
            lines.append("Concrete files already discovered:")
            lines.extend(f"- {uri}" for uri in progress.globbed_file_uris[:5])
        if progress.concrete_read_uris:
            lines.append("Concrete documents already read:")
            lines.extend(f"- {uri}" for uri in progress.concrete_read_uris[:5])
        if progress.relevant_evidence_blocks:
            lines.append("Relevant evidence already found:")
            for block in progress.relevant_evidence_blocks[:1]:
                preview = block.strip().splitlines()[0][:120]
                lines.append(f"- {preview}")

        if not progress.has_concrete_read and progress.globbed_file_uris:
            lines.append("Next step: read one or more concrete files from the discovered file list.")
        elif not progress.has_concrete_read:
            lines.append("Next step: find concrete files first; do not stop at scope summaries.")
        elif not progress.has_relevant_evidence:
            lines.append("Next step: continue narrowing to the directly relevant section before answering.")

        return "\n".join(lines)

    @staticmethod
    def _truncate_evidence_block(block: str, limit: int = 1800) -> str:
        """Trim large evidence blocks while keeping a natural boundary when possible."""
        normalized = block.strip()
        if len(normalized) <= limit:
            return normalized

        truncated = normalized[:limit]
        cut_points = [
            truncated.rfind("\n\n"),
            truncated.rfind("\n"),
            truncated.rfind("。"),
            truncated.rfind("；"),
        ]
        cut_at = max(cut_points)
        if cut_at >= int(limit * 0.6):
            truncated = truncated[: cut_at + 1]

        return f"{truncated.strip()}\n\n[文档节选]"

    @classmethod
    def _score_evidence_block(cls, user_request: str | None, title: str, content: str, uri: str) -> int:
        """Score a candidate evidence block against the user request."""
        query_terms = cls._extract_query_terms(user_request or "")
        title_lower = title.lower()
        content_lower = content.lower()
        uri_lower = uri.lower()

        title_hits = sum(1 for term in query_terms if term in title_lower)
        content_hits = sum(1 for term in query_terms if term in content_lower)
        uri_hits = sum(1 for term in query_terms if term in uri_lower)

        score = title_hits * 6 + content_hits * 2 + uri_hits
        if uri_lower.endswith("/.abstract.md"):
            score -= 6
        elif uri_lower.endswith("/.overview.md"):
            score -= 3
        if score > 0 and cls._contains_markdown_image(content):
            score += 2
        return score

    @classmethod
    def _collect_document_evidence_blocks(
        cls, user_request: str | None, tools_used: list[dict], limit: int = 3
    ) -> list[str]:
        """Collect the most relevant documentation evidence blocks for final answer generation."""
        candidates: list[tuple[int, str]] = []

        for tool_used in tools_used:
            if tool_used.get("tool_name") not in {"openviking_read", "openviking_grep"}:
                continue
            if not tool_used.get("execute_success"):
                continue

            result = tool_used.get("result")
            if not isinstance(result, str) or not result.strip():
                continue

            args = cls._parse_tool_args(tool_used)
            uri = str(args.get("uri") or args.get("target_uri") or "")
            if tool_used.get("tool_name") == "openviking_read" and is_generic_scope_summary_uri(uri):
                continue

            sections = cls._split_evidence_sections(result)
            if sections:
                scored_sections = sorted(
                    sections,
                    key=lambda section: cls._score_evidence_block(
                        user_request,
                        str(section["title"]),
                        str(section["content"]),
                        uri,
                    ),
                    reverse=True,
                )
                best_section = scored_sections[0]
                excerpt = cls._truncate_evidence_block(str(best_section["content"]))
                score = cls._score_evidence_block(
                    user_request,
                    str(best_section["title"]),
                    excerpt,
                    uri,
                )
            else:
                excerpt = cls._truncate_evidence_block(result)
                score = cls._score_evidence_block(user_request, "", excerpt, uri)

            if excerpt:
                candidates.append((score, excerpt))

        ranked_blocks: list[str] = []
        seen: set[str] = set()
        for _score, block in sorted(candidates, key=lambda item: item[0], reverse=True):
            normalized = cls._prepare_text_block_for_rewrite(block)
            if not normalized or normalized in seen:
                continue
            ranked_blocks.append(normalized)
            seen.add(normalized)
            if len(ranked_blocks) >= limit:
                break

        return ranked_blocks

    async def _build_document_grounded_text_reply(
        self,
        user_request: str | None,
        tools_used: list[dict],
        session_id: str | None = None,
    ) -> tuple[str | None, dict[str, int]]:
        """Generate the final user-facing KB reply from documentation evidence only."""
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        evidence_blocks = self._collect_document_evidence_blocks(user_request, tools_used)
        if not user_request or not evidence_blocks:
            return None, usage

        evidence_text = "\n\n".join(
            f"证据 {index}:\n{block}" for index, block in enumerate(evidence_blocks, start=1)
        )
        response = await self.provider.chat(
            messages=[
                {"role": "system", "content": self.context.build_kb_final_response_system_prompt()},
                {
                    "role": "user",
                    "content": (
                        f"用户问题：{user_request}\n\n"
                        f"文档证据：\n{evidence_text}\n\n"
                        "请基于这些证据直接写最终用户答复。"
                    ),
                },
            ],
            tools=None,
            model=self.model,
            max_tokens=900,
            temperature=0,
            session_id=f"{session_id}::kb-final" if session_id else None,
        )

        if response.usage:
            usage.update(response.usage)

        final_reply = (response.content or "").strip()
        if not final_reply:
            raise ValueError("KB final responder returned empty content.")
        return final_reply, usage

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
        hash_heading_re = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
        bold_heading_re = re.compile(r"(?m)^\*\*(?P<title>[^*\n][^*\n]{0,78}?)\*\*\s*$")

        matches: list[dict[str, str | int]] = []
        for match in hash_heading_re.finditer(content):
            matches.append(
                {
                    "start": match.start(),
                    "level": len(match.group(1)),
                    "title": match.group(2).strip(),
                }
            )

        for match in bold_heading_re.finditer(content):
            title = match.group("title").strip()
            if not title:
                continue
            matches.append(
                {
                    "start": match.start(),
                    "level": 1,
                    "title": title,
                }
            )

        matches.sort(key=lambda item: int(item["start"]))
        deduped_matches: list[dict[str, str | int]] = []
        seen_starts: set[int] = set()
        for match in matches:
            start = int(match["start"])
            if start in seen_starts:
                continue
            seen_starts.add(start)
            deduped_matches.append(match)
        matches = deduped_matches

        if not matches:
            return []

        sections: list[dict[str, str | int]] = []
        for index, match in enumerate(matches):
            level = int(match["level"])
            end = len(content)
            for next_match in matches[index + 1 :]:
                if int(next_match["level"]) <= level:
                    end = int(next_match["start"])
                    break

            section_text = content[int(match["start"]) : end].strip()
            if not section_text:
                continue

            sections.append(
                {
                    "title": str(match["title"]).strip(),
                    "level": level,
                    "content": section_text,
                }
            )

        return sections

    @classmethod
    def _format_selected_evidence_section(cls, section: dict[str, str | int]) -> str:
        """Normalize a selected evidence section into markdown-like form for rendering."""
        content = str(section.get("content", "")).strip()
        title = str(section.get("title", "")).strip()
        if not content:
            return ""
        if content.startswith("#") or not title:
            return content

        lines = content.splitlines()
        first_line = lines[0].strip() if lines else ""
        normalized_first_line = re.sub(r"^\*\*(.+)\*\*$", r"\1", first_line)
        if normalized_first_line != title:
            return content

        body = "\n".join(lines[1:]).strip()
        if not body:
            return f"# {title}"
        return f"# {title}\n\n{body}"

    @classmethod
    def _score_markdown_section(cls, section: dict[str, str | int], query_terms: list[str]) -> int:
        """Score a markdown section against the user request."""
        title = str(section["title"]).lower()
        content = str(section["content"]).lower()

        title_hits = sum(1 for term in query_terms if term in title)
        content_hits = sum(1 for term in query_terms if term in content)
        image_bonus = 2 if (title_hits or content_hits) and cls._contains_markdown_image(content) else 0

        return title_hits * 6 + content_hits * 2 + image_bonus

    @classmethod
    def _select_relevant_markdown_section(cls, user_request: str, content: str) -> str | None:
        """Select the most relevant heading-based section from a markdown document."""
        selected_sections = cls._select_relevant_markdown_sections(user_request, content)
        if len(selected_sections) != 1:
            return None
        return selected_sections[0]

    @staticmethod
    def _extract_draft_heading_hints(draft_reply: str | None) -> list[str]:
        """Extract section-heading hints from the agent draft reply."""
        if not isinstance(draft_reply, str) or not draft_reply.strip():
            return []
        return [
            line.strip()
            for line in re.findall(r"(?m)^#{1,6}\s+(.+)$", draft_reply)
            if line.strip()
        ]

    @classmethod
    def _score_section_against_heading_hint(cls, section_title: str, heading_hint: str) -> int:
        """Score how well a section title matches a heading hinted in the draft reply."""
        normalized_title = cls._normalize_section_title(section_title).lower()
        normalized_hint = cls._normalize_section_title(heading_hint).lower()
        if not normalized_title or not normalized_hint:
            return 0
        if normalized_title == normalized_hint:
            return 12
        if normalized_title in normalized_hint or normalized_hint in normalized_title:
            return 8

        title_terms = cls._extract_query_terms(normalized_title)
        hint_terms = cls._extract_query_terms(normalized_hint)
        overlap = len(set(title_terms) & set(hint_terms))
        return overlap * 3

    @classmethod
    def _select_relevant_markdown_sections(
        cls, user_request: str, content: str, draft_reply: str | None = None
    ) -> list[str]:
        """Select one or more relevant image-backed sections from parsed evidence content."""
        query_terms = cls._extract_query_terms(user_request)
        heading_hints = cls._extract_draft_heading_hints(draft_reply)
        if not query_terms and not heading_hints:
            return []

        sections = cls._split_evidence_sections(content)
        if not sections:
            return []

        eligible_sections = [
            section
            for section in sections
            if cls._contains_markdown_image(str(section.get("content", "")).strip())
        ]
        if not eligible_sections:
            return []

        selected: list[str] = []
        seen: set[str] = set()

        if heading_hints:
            for heading_hint in heading_hints:
                best_section: dict[str, str | int] | None = None
                best_score = 0
                for section in eligible_sections:
                    section_content = str(section["content"]).strip()
                    if section_content in seen:
                        continue
                    score = cls._score_section_against_heading_hint(str(section["title"]), heading_hint)
                    if query_terms:
                        score += cls._score_markdown_section(section, query_terms)
                    if score > best_score:
                        best_section = section
                        best_score = score
                if best_section and best_score > 0:
                    section_content = cls._format_selected_evidence_section(best_section)
                    selected.append(section_content)
                    seen.add(section_content)

        if selected:
            return selected

        best_overall_score = 0
        for section in sections:
            best_overall_score = max(best_overall_score, cls._score_markdown_section(section, query_terms))

        best_section: dict[str, str | int] | None = None
        best_score = 0
        for section in eligible_sections:
            score = cls._score_markdown_section(section, query_terms)
            if score > best_score:
                best_section = section
                best_score = score

        if not best_section or best_score <= 0:
            return []

        # Only keep image-grounded sections when they are close to the best textual match.
        if best_overall_score > 0 and best_score * 2 < best_overall_score:
            return []

        selected = cls._format_selected_evidence_section(best_section)
        if not cls._contains_markdown_image(selected):
            return []
        return [selected]

    @classmethod
    def _select_relevant_openviking_section(
        cls, user_request: str | None, tools_used: list[dict]
    ) -> str | None:
        """Select one unambiguous image-backed OpenViking section for final formatting."""
        selected_sections = cls._select_relevant_openviking_sections(user_request, tools_used)
        if len(selected_sections) != 1:
            return None
        return selected_sections[0]

    @classmethod
    def _select_relevant_openviking_sections(
        cls, user_request: str | None, tools_used: list[dict], draft_reply: str | None = None
    ) -> list[str]:
        """Select one or more image-backed OpenViking sections for final formatting."""
        if not user_request:
            return []

        query_terms = cls._extract_query_terms(user_request)
        heading_hints = cls._extract_draft_heading_hints(draft_reply)
        if not query_terms and not heading_hints:
            return []

        candidate_groups: list[list[str]] = []
        for tool_used in tools_used:
            if tool_used.get("tool_name") != "openviking_read":
                continue

            result = tool_used.get("result")
            if not isinstance(result, str) or not cls._contains_markdown_image(result):
                continue

            selected = cls._select_relevant_markdown_sections(user_request, result, draft_reply=draft_reply)
            if not selected:
                continue
            candidate_groups.append(selected)

        if len(candidate_groups) != 1:
            return []
        return candidate_groups[0]

    @staticmethod
    def _normalize_section_title(section: str) -> str:
        """Normalize a markdown heading into a user-facing title."""
        title = re.sub(r"^#{1,6}\s+", "", section.strip(), flags=re.MULTILINE)
        title = re.sub(r"^\*\*(.+)\*\*$", r"\1", title)
        title = title.splitlines()[0].strip() if title else ""
        title = re.sub(r"^\d+(?:\.\d+)+\s*", "", title)
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
    def _contains_markdown_image(text: str) -> bool:
        """Return whether a text block contains markdown image syntax."""
        return bool(MARKDOWN_IMAGE_RE.search(text))

    @staticmethod
    def _is_markdown_image_line(line: str) -> bool:
        """Return whether a line is a standalone markdown image line."""
        return bool(re.match(r"^!\[[^\]]*\]\([^)]+\)\s*$", line.strip()))

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

            if cls._is_markdown_image_line(stripped):
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
                if candidate.startswith("#") or cls._contains_markdown_image(candidate):
                    continue
                if re.match(r"^(\d+\.\s+|[-*]\s+)", candidate):
                    continue
                if re.fullmatch(r"(总结如下|如下|说明如下|答复如下)\s*[：:]?", candidate):
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
        if any(marker in normalized for marker in internal_markers):
            return True

        internal_patterns = [
            r"已从\s+.+\.(?:md|markdown|docx?|pdf|txt)\s+中?获取",
            r"(?:在|从)\s+[`\"']?[^`\"'\s]+\.(?:md|markdown|docx?|pdf|txt)[`\"']?\s+中(?:找到|检索到|读取到|定位到)",
            r"我已确认",
            r"可以直接用于回答",
            r"现在可给出最终答案",
            r"接下来(?:给出|提供)最终答案",
            r"现在我可以基于.+给用户",
            r"文档原文",
            r"根据.+文档",
            r"根据.+资料",
            r"已获取到.+完整",
            r"信息明确[，,、 ]+权威",
            r"以下(?:内容|结论).+来自",
        ]
        return any(re.search(pattern, text, re.IGNORECASE) for pattern in internal_patterns)

    @classmethod
    def _render_grounded_section(cls, section: str) -> list[str]:
        """Render one grounded section into readable reply parts."""
        title_line, intro_segments, steps = cls._extract_structured_section(section)
        section_title = cls._normalize_section_title(title_line) if title_line else ""
        rendered_intro = cls._render_segments(intro_segments)
        rendered_steps = [
            cls._render_segments(step["segments"])
            for step in steps
            if isinstance(step.get("segments"), list)
        ]
        rendered_steps = [step for step in rendered_steps if step]

        parts: list[str] = []
        if section_title:
            parts.append(f"## {section_title}")
        if rendered_intro:
            parts.append(rendered_intro)
        if rendered_steps:
            parts.extend(rendered_steps)
        elif not rendered_intro:
            rendered_body = cls._prepare_text_block_for_rewrite(section)
            if rendered_body:
                parts.append(rendered_body)
        return parts

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

        best_sections = self._select_relevant_openviking_sections(
            user_request, tools_used, draft_reply=draft_reply
        )
        if not best_sections:
            return None, usage

        parts: list[str] = []
        intro = self._extract_grounded_intro(draft_reply)
        if intro:
            parts.append(intro)

        for section in best_sections:
            parts.extend(self._render_grounded_section(section))

        return "\n\n".join(part for part in parts if part).strip(), usage

    async def _run_agent_loop(
        self,
        messages: list[dict],
        session_key: SessionKey,
        publish_events: bool = True,
        sender_id: str | None = None,
        user_request: str | None = None,
        require_document_evidence: bool = False,
        initial_tools_used: list[dict] | None = None,
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
        tools_used: list[dict] = list(initial_tools_used or [])
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
                        "content": self.context.build_tool_reflection_prompt(),
                    }
                )
            else:
                if require_document_evidence and self._is_knowledge_base_mode():
                    progress = self._analyze_kb_search_progress(user_request, tools_used)
                    if not progress.answer_ready and iteration < self.max_iterations:
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
                                    self._format_kb_search_progress(progress)
                                ),
                            }
                        )
                        continue

                final_content = response.content
                if self._is_knowledge_base_mode() and self._has_document_evidence(tools_used):
                    grounded_reply, rewrite_usage = await self._build_rewritten_openviking_reply(
                        user_request=user_request,
                        tools_used=tools_used,
                    )
                    if grounded_reply:
                        for key in token_usage:
                            token_usage[key] += rewrite_usage.get(key, 0)
                        final_content = grounded_reply
                    else:
                        grounded_text_reply, rewrite_usage = await self._build_document_grounded_text_reply(
                            user_request=user_request,
                            tools_used=tools_used,
                            session_id=session_key.safe_name(),
                        )
                        if grounded_text_reply:
                            for key in token_usage:
                                token_usage[key] += rewrite_usage.get(key, 0)
                            final_content = grounded_text_reply
                else:
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

            prefetched_tools_used: list[dict] = []
            if self._is_knowledge_base_mode():
                messages, prefetched_tools_used = await self._prefetch_knowledge_base_evidence(
                    messages=messages,
                    session_key=session_key,
                    sender_id=msg.sender_id,
                    user_request=msg.content,
                    publish_events=True,
                )

            # Run agent loop
            final_content, tools_used, token_usage = await self._run_agent_loop(
                messages=messages,
                session_key=session_key,
                publish_events=True,
                sender_id=msg.sender_id,
                user_request=msg.content,
                require_document_evidence=self._is_knowledge_base_mode(),
                initial_tools_used=prefetched_tools_used,
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
