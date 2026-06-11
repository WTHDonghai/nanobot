"""KB answer finalization helpers for :mod:`vikingbot.agent.loop`."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any
from urllib.parse import quote, unquote

from loguru import logger

from openviking_cli.resource_preview import (
    RESOURCE_PREVIEW_SECRET_ENV,
    ResourcePreviewTokenError,
    create_resource_preview_token,
)
from vikingbot.agent.kb_patterns import MARKDOWN_IMAGE_LINE_RE, SEND_IMAGE_LINE_RE
from vikingbot.config.schema import SessionKey
from vikingbot.openviking_mount.uri_utils import is_generic_scope_summary_uri, is_summary_uri


class KbResponseMixin:
    """Helpers that turn selected KB evidence into the final user-facing reply."""

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
            if not SEND_IMAGE_LINE_RE.search(content):
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
        for match in SEND_IMAGE_LINE_RE.finditer(content):
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
                is_image_line = bool(SEND_IMAGE_LINE_RE.fullmatch(line.strip()))
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
            block for block in selected_evidence_blocks if SEND_IMAGE_LINE_RE.search(block)
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
            f"draft_has_images={bool(MARKDOWN_IMAGE_LINE_RE.search(str(draft_content or '')))} "
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
            text_without_images = SEND_IMAGE_LINE_RE.sub("", segment)
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
            logger.warning("Resource preview links disabled because no preview secret is configured")
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

    @classmethod
    def _extract_read_evidence_uris(cls, messages: list[dict]) -> list[str]:
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
                or cls._classify_tool_error_result(content)
            ):
                continue

            tool_call_id = message.get("tool_call_id")
            if not tool_call_id:
                continue

            args = cls._find_tool_call_arguments(messages[:message_index], tool_call_id)
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
