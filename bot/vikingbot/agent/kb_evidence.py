"""KB evidence selection helpers for :mod:`vikingbot.agent.loop`."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from loguru import logger

from vikingbot.agent.kb_markdown import KbMarkdownMixin
from vikingbot.agent.kb_tool_evidence import KbToolEvidenceMixin
from vikingbot.config.schema import SessionKey


@dataclass
class _SemanticEvidenceSelection:
    """Relevant sections plus a semantic coverage decision for the request."""

    sections: list[str]
    coverage: str = "unknown"
    missing: str = ""
    next_query: str = ""


class KbEvidenceMixin(KbMarkdownMixin, KbToolEvidenceMixin):
    """Helpers that select KB evidence for grounded answers."""

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

    @classmethod
    def _parse_section_selection_indexes(cls, selection_text: str, max_index: int) -> list[int]:
        """Parse selected section indexes from JSON or plain-number model output."""
        indexes, _is_valid = cls._parse_section_selection_response(
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
        memory_hints: str = "",
        max_blocks: int,
    ) -> str:
        """Build one cross-document evidence selector prompt for the fast batch path."""
        candidates: list[str] = []
        for index, candidate in enumerate(candidate_sections, start=1):
            text = cls._prepare_text_block_for_rewrite(candidate.get("text", ""))
            if len(text) > 800:
                text = f"{text[:800]}..."
            candidates.append(f"[{index}] Source URI: {candidate.get('uri', '')}\n{text}")

        memory_section = ""
        if str(memory_hints or "").strip():
            memory_section = (
                "Agent memory hints for retrieval only. They may help interpret search wording "
                "or likely document areas, but they are not evidence and must not be used as "
                "facts in the answer:\n"
                f"{memory_hints.strip()}\n\n"
            )

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
            f"{memory_section}"
            "Candidate sections:\n"
            + "\n\n".join(candidates)
        )

    async def _collect_fast_batch_evidence_selection(
        self,
        user_request: str,
        tools_used: list[dict[str, Any]],
        session_key: SessionKey,
        *,
        memory_hints: str = "",
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
                            memory_hints=memory_hints,
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
