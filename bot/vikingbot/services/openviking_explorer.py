"""Low-level OpenViking filesystem exploration helpers for MCP consumers."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from vikingbot.openviking_mount.ov_server import VikingClient
from vikingbot.openviking_mount.uri_utils import is_generic_scope_summary_uri, is_summary_uri
from vikingbot.utils.helpers import get_mcp_artifacts_path

DEFAULT_TARGET_URI = "viking://resources/"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff"}
LOCAL_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")


class OpenVikingExplorerService:
    """Expose filesystem-style OpenViking retrieval in a MCP-friendly form."""

    def __init__(
        self,
        client_factory: Callable[[str | None], Awaitable[VikingClient]] | None = None,
        artifact_root: Path | None = None,
        agent_id: str | None = None,
    ) -> None:
        self._client_factory = client_factory or VikingClient.create
        self._artifact_root = artifact_root
        self._agent_id = agent_id

    async def openviking_list(
        self,
        *,
        uri: str = DEFAULT_TARGET_URI,
        recursive: bool = False,
        context_id: str | None = None,
    ) -> dict[str, Any]:
        client = await self._client_factory(context_id or self._agent_id)
        try:
            normalized_uri = str(uri or DEFAULT_TARGET_URI).strip() or DEFAULT_TARGET_URI
            entries = await client.list_resources(path=normalized_uri, recursive=recursive)
            normalized_entries = sorted(
                [
                    {
                        "name": str(entry.get("name") or ""),
                        "uri": str(entry.get("uri") or ""),
                        "is_dir": bool(entry.get("isDir")),
                        "size": int(entry.get("size") or 0),
                    }
                    for entry in entries or []
                ],
                key=lambda entry: (not entry["is_dir"], entry["name"].lower(), entry["uri"]),
            )
            return {
                "uri": normalized_uri,
                "recursive": recursive,
                "count": len(normalized_entries),
                "entries": normalized_entries,
            }
        finally:
            await client.close()

    async def openviking_glob(
        self,
        *,
        pattern: str,
        uri: str = DEFAULT_TARGET_URI,
        context_id: str | None = None,
    ) -> dict[str, Any]:
        client = await self._client_factory(context_id or self._agent_id)
        try:
            normalized_uri = str(uri or DEFAULT_TARGET_URI).strip() or DEFAULT_TARGET_URI
            result = await client.glob(pattern, uri=normalized_uri or None)
            matches = result.get("matches", []) if isinstance(result, dict) else getattr(result, "matches", [])
            count = result.get("count", len(matches)) if isinstance(result, dict) else getattr(result, "count", len(matches))
            normalized_matches = [str(match.get("uri") if isinstance(match, dict) else match or "") for match in matches]
            normalized_matches = [match for match in normalized_matches if match]
            return {
                "pattern": pattern,
                "uri": normalized_uri,
                "count": int(count or len(normalized_matches)),
                "matches": normalized_matches,
            }
        finally:
            await client.close()

    async def openviking_search(
        self,
        *,
        query: str,
        target_uri: str = DEFAULT_TARGET_URI,
        context_id: str | None = None,
    ) -> dict[str, Any]:
        client = await self._client_factory(context_id or self._agent_id)
        try:
            normalized_target_uri = str(target_uri or DEFAULT_TARGET_URI).strip() or DEFAULT_TARGET_URI
            results = await client.search(query, target_uri=normalized_target_uri)
            resources = results.get("resources") or []
            ordered_resources = [
                resource
                for _, resource in sorted(
                    enumerate(resources),
                    key=lambda item: (
                        self._resource_kind_rank(query, item[1]),
                        -self._resource_score(item[1]),
                        item[0],
                    ),
                )
            ]

            documents: list[dict[str, Any]] = []
            summaries: list[dict[str, Any]] = []
            images: list[dict[str, Any]] = []

            for resource in ordered_resources:
                item = self._normalize_search_resource(resource)
                if item["kind"] == "image":
                    images.append(item)
                elif item["kind"] in {"summary", "scope_summary"}:
                    summaries.append(item)
                else:
                    documents.append(item)

            payload: dict[str, Any] = {
                "query": query,
                "target_uri": normalized_target_uri,
                "total": int(results.get("total", len(resources)) or 0),
                "documents": documents,
                "images": images,
                "summaries": summaries,
            }

            memories = [
                self._normalize_aux_context(item)
                for item in (results.get("memories") or [])
            ]
            skills = [
                self._normalize_aux_context(item)
                for item in (results.get("skills") or [])
            ]
            if memories:
                payload["memories"] = memories
            if skills:
                payload["skills"] = skills

            payload["next_step"] = self._build_search_next_step(
                query=query,
                documents=documents,
                summaries=summaries,
                images=images,
            )
            return payload
        finally:
            await client.close()

    async def openviking_read(
        self,
        *,
        uri: str,
        level: str = "read",
        include_images: bool = True,
        max_images: int = 8,
        context_id: str | None = None,
    ) -> dict[str, Any]:
        client = await self._client_factory(context_id or self._agent_id)
        try:
            normalized_uri = str(uri or "").strip()
            if not normalized_uri:
                return {
                    "uri": "",
                    "level": level,
                    "kind": "missing",
                    "note": "Missing URI.",
                    "content_markdown": "",
                    "candidate_uris": [],
                }

            if level != "read":
                read_target = self._normalize_non_read_uri(normalized_uri, level)
                content = await client.read_content(read_target, level=level)
                return {
                    "uri": normalized_uri,
                    "level": level,
                    "kind": "summary" if is_summary_uri(normalized_uri) else "document",
                    "resolved_uri": read_target,
                    "note": "",
                    "candidate_uris": [],
                    "content_markdown": content,
                }

            if is_summary_uri(normalized_uri):
                parent_uri = normalized_uri.rstrip("/").rsplit("/", 1)[0]
                resolved_uri, candidate_uris = await client.resolve_read_uri(parent_uri)
                deduped_candidates = self._dedupe_candidate_uris(candidate_uris, preferred=resolved_uri)
                return {
                    "uri": normalized_uri,
                    "level": "read",
                    "kind": "scope_summary" if is_generic_scope_summary_uri(normalized_uri) else "summary",
                    "resolved_uri": resolved_uri,
                    "note": (
                        "This is summary content for navigation only. Read one concrete URI from candidate_uris before drafting."
                    ),
                    "candidate_uris": deduped_candidates,
                    "content_markdown": await client.read_content(normalized_uri, level="read"),
                }

            stat = await client.stat(normalized_uri)
            if not stat:
                return {
                    "uri": normalized_uri,
                    "level": "read",
                    "kind": "missing",
                    "note": f"File not found: {normalized_uri}",
                    "candidate_uris": [],
                    "content_markdown": "",
                }

            if self._is_image_like_target(normalized_uri, stat):
                images = await client.export_uri_images(
                    normalized_uri,
                    output_dir=self._artifact_dir(),
                    max_images=max_images,
                )
                return {
                    "uri": normalized_uri,
                    "level": "read",
                    "kind": "image_directory" if stat.get("isDir") else "image",
                    "resolved_uri": normalized_uri.rstrip("/"),
                    "note": "",
                    "candidate_uris": [],
                    "image_count": len(images),
                    "content_markdown": self._render_local_images(images),
                }

            read_uri = normalized_uri.rstrip("/")
            candidate_uris: list[str] = []
            kind = "document"
            if stat.get("isDir"):
                resolved_uri, resolved_candidates = await client.resolve_read_uri(read_uri)
                candidate_uris = self._dedupe_candidate_uris(resolved_candidates, preferred=resolved_uri)
                if not resolved_uri:
                    return {
                        "uri": normalized_uri,
                        "level": "read",
                        "kind": "directory",
                        "note": "Directory cannot be read directly. Pick one concrete text URI from candidate_uris.",
                        "candidate_uris": candidate_uris,
                        "content_markdown": "",
                    }
                read_uri = resolved_uri
                kind = "directory_document"

            content = await client.read_content(read_uri, level="read")
            if not content:
                return {
                    "uri": normalized_uri,
                    "level": "read",
                    "kind": kind,
                    "resolved_uri": read_uri,
                    "note": "No readable content was returned.",
                    "candidate_uris": candidate_uris,
                    "content_markdown": "",
                }

            if not include_images or max_images <= 0:
                return {
                    "uri": normalized_uri,
                    "level": "read",
                    "kind": kind,
                    "resolved_uri": read_uri,
                    "note": "",
                    "candidate_uris": candidate_uris,
                    "content_markdown": content,
                }

            materialized = await client.materialize_inline_image_refs_to_directory(
                content,
                read_uri,
                self._artifact_dir(),
            )
            normalized_content = self._normalize_fragment_markdown(materialized)
            local_image_count = self._count_local_images(normalized_content)
            if local_image_count > 0:
                return {
                    "uri": normalized_uri,
                    "level": "read",
                    "kind": kind,
                    "resolved_uri": read_uri,
                    "note": "",
                    "candidate_uris": candidate_uris,
                    "image_count": local_image_count,
                    "content_markdown": normalized_content,
                }

            related_images = await client.export_related_images(
                read_uri,
                output_dir=self._artifact_dir(),
                max_images=max_images,
            )
            content_markdown = self._append_related_images(content, related_images)
            note = ""
            if related_images:
                note = "No inline image anchors were materialized, so nearby extracted images were appended."
            return {
                "uri": normalized_uri,
                "level": "read",
                "kind": kind,
                "resolved_uri": read_uri,
                "note": note,
                "candidate_uris": candidate_uris,
                "image_count": len(related_images),
                "content_markdown": content_markdown,
            }
        finally:
            await client.close()

    def _artifact_dir(self) -> Path:
        root = self._artifact_root or get_mcp_artifacts_path("openviking-explorer")
        root.mkdir(parents=True, exist_ok=True)
        return root

    @staticmethod
    def _normalize_non_read_uri(uri: str, level: str) -> str:
        normalized_uri = uri.rstrip("/")
        if level == "abstract" and normalized_uri.endswith("/.abstract.md"):
            return normalized_uri[: -len("/.abstract.md")]
        if level == "overview" and normalized_uri.endswith("/.overview.md"):
            return normalized_uri[: -len("/.overview.md")]
        if normalized_uri.endswith("/.abstract.md") or normalized_uri.endswith("/.overview.md"):
            return normalized_uri.rsplit("/", 1)[0]
        return normalized_uri

    @staticmethod
    def _is_image_like_target(uri: str, stat: dict[str, Any]) -> bool:
        normalized_uri = uri.rstrip("/")
        if stat.get("isDir"):
            return normalized_uri.endswith("/_images") or normalized_uri.endswith("_images")
        return Path(normalized_uri).suffix.lower() in IMAGE_EXTENSIONS

    @staticmethod
    def _render_local_images(images: list[dict[str, str]]) -> str:
        return "\n".join(
            f"![{(str(image.get('caption') or Path(str(image.get('local_path') or '')).stem or 'image')).strip()}]({image.get('local_path')})"
            for image in images
            if str(image.get("local_path") or "").strip()
        )

    @classmethod
    def _append_related_images(cls, content: str, images: list[dict[str, str]]) -> str:
        image_block = cls._render_local_images(images)
        if not image_block:
            return content
        return f"{content}\n\n---\n\n相关图片：\n{image_block}"

    @staticmethod
    def _normalize_fragment_markdown(content: str) -> str:
        lines = [line.rstrip() for line in str(content or "").splitlines()]
        normalized: list[str] = []
        previous_blank = False
        for line in lines:
            if not line.strip():
                if normalized and not previous_blank:
                    normalized.append("")
                previous_blank = True
                continue
            normalized.append(line)
            previous_blank = False
        while normalized and not normalized[-1].strip():
            normalized.pop()
        return "\n".join(normalized).strip()

    @classmethod
    def _count_local_images(cls, content: str) -> int:
        count = 0
        for match in LOCAL_MARKDOWN_IMAGE_RE.finditer(str(content or "")):
            ref = match.group(1).strip()
            if ref and "://" not in ref:
                count += 1
        return count

    @staticmethod
    def _dedupe_candidate_uris(candidate_uris: list[str], preferred: str | None = None) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in [preferred, *(candidate_uris or [])]:
            normalized = str(candidate or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(normalized)
        return deduped

    @staticmethod
    def _normalize_aux_context(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "uri": str(item.get("uri") or ""),
            "abstract": str(item.get("abstract") or "").strip(),
        }

    @classmethod
    def _normalize_search_resource(cls, resource: dict[str, Any]) -> dict[str, Any]:
        uri = str(resource.get("uri") or "").strip()
        return {
            "uri": uri,
            "kind": cls._resource_kind(uri),
            "score": cls._resource_score(resource),
            "match_reason": str(resource.get("match_reason") or "").strip(),
            "abstract": str(resource.get("abstract") or "").strip(),
            "category": str(resource.get("category") or "").strip(),
            "context_type": str(resource.get("context_type") or "").strip(),
        }

    @classmethod
    def _resource_kind(cls, uri: str) -> str:
        if cls._is_image_uri(uri):
            return "image"
        if is_generic_scope_summary_uri(uri):
            return "scope_summary"
        if is_summary_uri(uri):
            return "summary"
        return "document"

    @staticmethod
    def _is_image_uri(uri: str) -> bool:
        return Path(str(uri or "").rstrip("/")).suffix.lower() in IMAGE_EXTENSIONS

    @staticmethod
    def _resource_score(resource: dict[str, Any]) -> float:
        try:
            return float(resource.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _is_image_focused_query(query: str) -> bool:
        normalized = str(query or "").strip().lower()
        if not normalized:
            return False
        zh_terms = ("图片", "照片", "截图", "图示", "图表", "配图", "示意图")
        en_terms = ("screenshot", "image", "photo", "figure", "diagram", "chart")
        return any(term in query for term in zh_terms) or any(term in normalized for term in en_terms)

    @classmethod
    def _resource_kind_rank(cls, query: str, resource: dict[str, Any]) -> int:
        uri = str(resource.get("uri") or "")
        is_image = cls._is_image_uri(uri)
        is_summary = is_summary_uri(uri)
        is_generic_summary = is_generic_scope_summary_uri(uri)

        if cls._is_image_focused_query(query):
            if is_image:
                return 0
            if not is_summary:
                return 1
            if not is_generic_summary:
                return 2
            return 3

        if not is_image and not is_summary:
            return 0
        if is_image:
            return 1
        if not is_generic_summary:
            return 2
        return 3

    @staticmethod
    def _build_search_next_step(
        *,
        query: str,
        documents: list[dict[str, Any]],
        summaries: list[dict[str, Any]],
        images: list[dict[str, Any]],
    ) -> str:
        if documents:
            if images:
                return (
                    "Read the most relevant concrete document URI with openviking_read. If the answer needs screenshots or figures, also read the most relevant image URI or image-bearing document."
                )
            return "Read the most relevant concrete document URI with openviking_read before drafting."
        if summaries:
            return (
                "Only summary results were found. Use openviking_glob or openviking_list in this scope, then openviking_read one concrete file."
            )
        if images:
            return "No concrete document was found, but image assets matched. Read the most relevant image URI with openviking_read."
        return f"No concrete result was found for '{query}'. Try a narrower target_uri or a more specific query."
