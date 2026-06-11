"""Trace summarization helpers for :mod:`vikingbot.agent.loop`."""

from __future__ import annotations

import re
from typing import Any


class LoopTraceMixin:
    """Pure helpers used to summarize tool activity in agent-loop traces."""

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
