"""Markdown section helpers for KB evidence selection."""

from __future__ import annotations

import re
from typing import Any

from vikingbot.agent.kb_patterns import (
    MARKDOWN_HEADING_RE,
    MARKDOWN_IMAGE_LINE_RE,
    NUMBERED_HEADING_RE,
)


class KbMarkdownMixin:
    """Helpers that split imported Markdown-like KB text into answerable sections."""

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
        if MARKDOWN_HEADING_RE.match(line):
            return True
        stripped = line.strip()
        if len(stripped) > 120:
            return False
        if cls._is_toc_heading_candidate(stripped, previous_lines):
            return False
        return bool(NUMBERED_HEADING_RE.match(stripped))

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
    def _prepare_text_block_for_rewrite(cls, text: str) -> str:
        """Clean imported Markdown noise while preserving answerable content."""
        cleaned_lines: list[str] = []
        for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
            line = raw_line.strip()
            if not line:
                cleaned_lines.append("")
                continue
            if MARKDOWN_IMAGE_LINE_RE.fullmatch(line):
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
