"""OpenViking file system tools: read, write, list, search resources."""

import asyncio
from abc import ABC
from pathlib import Path
import re
from typing import Any, Optional, Union

import httpx
from loguru import logger

from vikingbot.agent.tools.base import Tool, ToolContext
from vikingbot.openviking_mount.uri_utils import is_generic_scope_summary_uri, is_summary_uri
from vikingbot.openviking_mount.ov_server import VikingClient

MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
WORD_IMAGE_ARTIFACT_RE = re.compile(r"ov-asset://|INCLUDEPICTURE", re.IGNORECASE)


class OVFileTool(Tool, ABC):
    def __init__(self):
        super().__init__()
        self._client = None

    async def _get_client(self, tool_context: ToolContext):
        if self._client is None:
            self._client = await VikingClient.create(tool_context.workspace_id)
        return self._client

class VikingReadTool(OVFileTool):
    """Tool to read content from Viking resources."""

    @property
    def name(self) -> str:
        return "openviking_read"

    @property
    def description(self) -> str:
        return (
            "Read content from OpenViking resources at different levels (abstract, overview, or "
            "full content). When the target is an image file or image directory, return sendable "
            "Markdown image lines instead of raw binary. For imported DOCX/PDF directory resources, "
            "prefer reading the matched child text resource (such as a split .md chapter) instead "
            "of the directory root when you need full text plus inline images."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "The Viking file's URI to read from (e.g., viking://resources/path/123.md)",
                },
                "level": {
                    "type": "string",
                    "description": "Reading level: 'abstract' (L0 summary), 'overview' (L1 overview), or 'read' (L2 full content)",
                    "enum": ["abstract", "overview", "read"],
                    "default": "abstract",
                },
                "include_images": {
                    "type": "boolean",
                    "description": "When level='read', include sendable image references extracted from the document when available.",
                    "default": True,
                },
                "max_images": {
                    "type": "integer",
                    "description": "Maximum number of document images to expose when include_images=true.",
                    "default": 4,
                },
            },
            "required": ["uri"],
        }

    @staticmethod
    def _normalize_non_read_uri(uri: str, level: str) -> str:
        """Map summary file URIs back to their parent directory for L0/L1 reads."""
        normalized_uri = uri.rstrip("/")
        if level == "abstract" and normalized_uri.endswith("/.abstract.md"):
            return normalized_uri[: -len("/.abstract.md")]
        if level == "overview" and normalized_uri.endswith("/.overview.md"):
            return normalized_uri[: -len("/.overview.md")]
        if normalized_uri.endswith("/.abstract.md") or normalized_uri.endswith("/.overview.md"):
            return normalized_uri.rsplit("/", 1)[0]
        return normalized_uri

    async def execute(
        self,
        tool_context: ToolContext,
        uri: str,
        level: str = "abstract",
        include_images: bool = True,
        max_images: int = 4,
        **kwargs: Any,
    ) -> str:
        try:
            client = await self._get_client(tool_context)
            if level != "read":
                normalized_uri = self._normalize_non_read_uri(uri, level)
                return await client.read_content(normalized_uri, level=level)

            if is_summary_uri(uri):
                summary_uri = uri.rstrip("/")
                parent_uri = summary_uri.rsplit("/", 1)[0]
                resolved = await client.resolve_read_uri(parent_uri)
                resolved_uri, candidate_uris = self._normalize_resolve_result(resolved)
                content = await client.read_content(summary_uri, level="read")
                if resolved_uri and resolved_uri not in candidate_uris:
                    candidate_uris = [resolved_uri, *candidate_uris]

                prefix = "这是作用域级摘要，不是具体文档正文" if is_generic_scope_summary_uri(
                    uri
                ) else "这是目录/章节摘要，不是具体文档正文"
                lines = [
                    f"{prefix}：{uri}",
                    "不要直接根据这段摘要回答用户问题。",
                ]

                deduped_candidates: list[str] = []
                seen_candidates: set[str] = set()
                for candidate_uri in candidate_uris:
                    normalized_candidate = str(candidate_uri or "").strip()
                    if not normalized_candidate or normalized_candidate in seen_candidates:
                        continue
                    seen_candidates.add(normalized_candidate)
                    deduped_candidates.append(normalized_candidate)

                if deduped_candidates:
                    if len(deduped_candidates) == 1:
                        lines.append("请下一步改为读取以下正文 URI：")
                    else:
                        lines.append("请下一步从以下正文 URI 中选择最相关的一项继续读取：")
                    lines.extend(
                        f"{index}. {candidate_uri}"
                        for index, candidate_uri in enumerate(deduped_candidates[:8], start=1)
                    )
                else:
                    lines.append("请先用 openviking_glob 查找具体文件，或缩小 target_uri 后重新 search。")

                if content:
                    lines.extend(
                        [
                            "",
                            "摘要内容（仅供定位，不可直接作答）:",
                            content,
                        ]
                    )
                return "\n".join(lines)

            stat = await client.stat(uri)
            if self._is_image_like_target(uri, stat):
                if not include_images or max_images <= 0:
                    return f"图片资源 {uri} 需要在 include_images=true 时读取。"
                direct_image_refs = await client.export_uri_for_send(uri, max_images=max_images)
                if not direct_image_refs:
                    return f"未能导出图片资源 {uri}。"
                return (
                    "可直接发送给用户的图片如下。请在最终回复中原样保留下面的 Markdown 图片行：\n"
                    + "\n".join(direct_image_refs)
                )

            read_uri = uri
            if stat.get("isDir"):
                resolved_uri, candidate_uris = await client.resolve_read_uri(uri)
                if not resolved_uri:
                    if candidate_uris:
                        candidates = "\n".join(
                            f"{index}. {candidate_uri}"
                            for index, candidate_uri in enumerate(candidate_uris[:8], start=1)
                        )
                        return (
                            f"目录资源 {uri} 不能直接执行 level='read'。"
                            "请改为读取以下正文 URI 之一：\n"
                            f"{candidates}"
                        )
                    return f"目录资源 {uri} 下没有可读取的正文文件。"
                read_uri = resolved_uri

            content = await client.read_content(read_uri, level=level)
            if not include_images or max_images <= 0 or not content:
                return content

            raw_content = content
            materialized_content = await client.materialize_inline_image_refs(content, read_uri)
            if isinstance(materialized_content, str):
                content = materialized_content
            if MARKDOWN_IMAGE_RE.search(content):
                return content

            image_refs = await client.export_related_images_for_send(read_uri, max_images=max_images)
            if not image_refs:
                return content

            guidance = (
                "可发送图片（如果需要把文档图片一并回复给用户，请在最终回复中原样保留下面的 Markdown 图片行）:"
            )
            return f"{content}\n\n---\n\n{guidance}\n" + "\n".join(image_refs)
        except Exception as e:
            return f"Error reading from Viking: {str(e)}"

    @staticmethod
    def _is_image_like_target(uri: str, stat: dict[str, Any]) -> bool:
        normalized_uri = uri.rstrip("/")
        if stat.get("isDir"):
            return normalized_uri.endswith("/_images") or normalized_uri.endswith("_images")
        return Path(normalized_uri).suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".svg",
            ".tiff",
        }

    @staticmethod
    def _normalize_resolve_result(result: Any) -> tuple[Optional[str], list[str]]:
        """Best-effort normalize resolve_read_uri results for mocks and runtime clients."""
        if isinstance(result, tuple) and len(result) == 2:
            resolved_uri, candidate_uris = result
            if not isinstance(candidate_uris, list):
                candidate_uris = list(candidate_uris or [])
            return resolved_uri, candidate_uris
        return None, []
class VikingListTool(OVFileTool):
    """Tool to list Viking resources."""

    @property
    def name(self) -> str:
        return "openviking_list"

    @property
    def description(self) -> str:
        return "List resources in a OpenViking folder path."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "The parent Viking uri to list (e.g., viking://resources/)",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Whether to list recursively",
                    "default": False,
                },
            },
            "required": ["uri"],
        }

    async def execute(
        self, tool_context: "ToolContext", uri: str, recursive: bool = False, **kwargs: Any
    ) -> str:
        try:
            client = await self._get_client(tool_context)
            entries = await client.list_resources(path=uri, recursive=recursive)

            if not entries:
                return f"No resources found at {uri}"

            result = []
            for entry in entries:
                item = {
                    "name": entry["name"],
                    "size": entry["size"],
                    "uri": entry["uri"],
                    "isDir": entry["isDir"],
                }
                result.append(str(item))
            return "\n".join(result)
        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            return f"Error listing Viking resources: {str(e)}"


class VikingSearchTool(OVFileTool):
    """Tool to search Viking resources."""

    @property
    def name(self) -> str:
        return "openviking_search"

    @property
    def description(self) -> str:
        return (
            "Search for resources in OpenViking using a query. This tool is for retrieval only: "
            "it returns candidate URIs, not final evidence. After finding a relevant document/text "
            "resource, call openviking_read on that URI before answering. Image assets under "
            "/_images/ may also be relevant evidence for image-focused queries."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "target_uri": {
                    "type": "string",
                    "description": "Optional target URI to limit search scope, if is None, then search the entire range.(e.g., viking://resources/)",
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        tool_context: "ToolContext",
        query: str,
        target_uri: Optional[str] = "",
        **kwargs: Any,
    ) -> str:
        try:
            client = await self._get_client(tool_context)
            raw_results = await client.search(query, target_uri=target_uri)
            results = self._normalize_search_results(client=client, results=raw_results, query=query, target_uri=target_uri)

            if not results:
                return f"No results found for query: {query}"
            if isinstance(results, list):
                result_strs = []
                for i, result in enumerate(results, 1):
                    result_strs.append(f"{i}. {str(result)}")
                return "\n".join(result_strs)
            if isinstance(results, dict):
                return self._format_search_results(query=query, results=results, target_uri=target_uri)
            return str(results)
        except Exception as e:
            return f"Error searching Viking: {str(e)}"

    @staticmethod
    def _normalize_search_results(
        client: Any, results: Any, query: str, target_uri: Optional[str] = ""
    ) -> Any:
        """Normalize raw search results to the dict shape expected by the formatter."""
        if isinstance(results, dict) or isinstance(results, list):
            return results

        if hasattr(results, "resources") or hasattr(results, "memories") or hasattr(results, "skills"):
            def _convert(items: Any) -> list[dict[str, Any]]:
                converted: list[dict[str, Any]] = []
                for item in items or []:
                    if hasattr(client, "_matched_context_to_dict"):
                        converted.append(client._matched_context_to_dict(item))
                        continue
                    converted.append(
                        {
                            "uri": getattr(item, "uri", ""),
                            "context_type": str(getattr(item, "context_type", "")),
                            "is_leaf": getattr(item, "is_leaf", False),
                            "abstract": getattr(item, "abstract", ""),
                            "overview": getattr(item, "overview", None),
                            "category": getattr(item, "category", ""),
                            "score": getattr(item, "score", 0.0),
                            "match_reason": getattr(item, "match_reason", ""),
                            "relations": getattr(item, "relations", []),
                        }
                    )
                return converted

            return {
                "memories": _convert(getattr(results, "memories", [])),
                "resources": _convert(getattr(results, "resources", [])),
                "skills": _convert(getattr(results, "skills", [])),
                "total": getattr(results, "total", len(getattr(results, "resources", []) or [])),
                "query": query,
                "target_uri": target_uri,
            }

        return results

    @staticmethod
    def _is_image_uri(uri: str) -> bool:
        return Path(uri.rstrip("/")).suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".svg",
            ".tiff",
        }

    @staticmethod
    def _is_summary_uri(uri: str) -> bool:
        return is_summary_uri(uri)

    @classmethod
    def _is_generic_scope_summary_uri(cls, uri: str) -> bool:
        return is_generic_scope_summary_uri(uri)

    @staticmethod
    def _resource_score(resource: dict[str, Any]) -> float:
        try:
            return float(resource.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _is_image_focused_query(query: str) -> bool:
        normalized = (query or "").strip().lower()
        if not normalized:
            return False

        zh_terms = (
            "图片",
            "照片",
            "截图",
            "图示",
            "图表",
            "配图",
            "示意图",
        )
        en_terms = (
            "screenshot",
            "image",
            "photo",
            "figure",
            "diagram",
            "chart",
        )

        return any(term in query for term in zh_terms) or any(term in normalized for term in en_terms)

    @classmethod
    def _resource_kind_rank(cls, query: str, resource: dict[str, Any]) -> int:
        uri = resource.get("uri", "")
        is_image = cls._is_image_uri(uri)
        is_summary = cls._is_summary_uri(uri)
        is_generic_summary = cls._is_generic_scope_summary_uri(uri)

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

    @classmethod
    def _format_search_results(
        cls, query: str, results: dict[str, Any], target_uri: Optional[str] = ""
    ) -> str:
        resources = results.get("resources") or []
        memories = results.get("memories") or []
        skills = results.get("skills") or []
        image_focused_query = cls._is_image_focused_query(query)
        ordered_resources = [
            resource
            for _, resource in sorted(
                enumerate(resources),
                key=lambda item: (
                    cls._resource_kind_rank(query, item[1]),
                    -cls._resource_score(item[1]),
                    item[0],
                ),
            )
        ]
        document_resources = [
            resource for resource in ordered_resources if not cls._is_image_uri(resource.get("uri", ""))
        ]
        concrete_document_resources = [
            resource
            for resource in document_resources
            if not cls._is_summary_uri(resource.get("uri", ""))
        ]
        summary_resources = [
            resource
            for resource in document_resources
            if cls._is_summary_uri(resource.get("uri", ""))
        ]
        image_resources = [
            resource for resource in ordered_resources if cls._is_image_uri(resource.get("uri", ""))
        ]

        lines = [f"OpenViking search query: {query}"]
        if target_uri:
            lines.append(f"Target URI: {target_uri}")
        lines.append(f"Total matches: {results.get('total', len(resources))}")

        def append_document_section() -> None:
            if not concrete_document_resources:
                return
            lines.append("")
            lines.append("Documents:")
            for idx, resource in enumerate(concrete_document_resources, start=1):
                uri = resource.get("uri", "")
                match_reason = (resource.get("match_reason") or "").strip()

                lines.append(f"{idx}. [document] {uri}")
                if match_reason:
                    lines.append(f"   Match reason: {match_reason}")
                lines.append("   Content preview omitted. Use openviking_read for evidence.")

        def append_image_section() -> None:
            if not image_resources:
                return
            lines.append("")
            lines.append("Image assets:")
            for idx, resource in enumerate(image_resources, start=1):
                uri = resource.get("uri", "")
                match_reason = (resource.get("match_reason") or "").strip()

                lines.append(f"{idx}. [image asset] {uri}")
                if match_reason:
                    lines.append(f"   Match reason: {match_reason}")
                lines.append(
                    "   Preview omitted. Use openviking_read(level='read', include_images=true) "
                    "on this URI to get sendable Markdown image lines."
                )

        if image_focused_query:
            append_image_section()
            append_document_section()
        else:
            append_document_section()
            append_image_section()

        if summary_resources:
            lines.append("")
            lines.append("Summaries:")
            for idx, resource in enumerate(summary_resources, start=1):
                uri = resource.get("uri", "")
                match_reason = (resource.get("match_reason") or "").strip()

                lines.append(f"{idx}. [summary] {uri}")
                if match_reason:
                    lines.append(f"   Match reason: {match_reason}")
                if cls._is_generic_scope_summary_uri(uri):
                    lines.append("   Generic scope summary only. Not a concrete document.")
                else:
                    lines.append("   Summary only. Not a concrete document.")
                lines.append("   Do not answer from this alone; locate a concrete file first.")

        if memories:
            lines.append("")
            lines.append("Memories:")
            for idx, memory in enumerate(memories, start=1):
                uri = memory.get("uri", "")
                abstract = (memory.get("abstract") or "").strip()
                lines.append(f"{idx}. {uri}")
                if abstract:
                    lines.append(f"   Abstract: {abstract}")

        if skills:
            lines.append("")
            lines.append("Skills:")
            for idx, skill in enumerate(skills, start=1):
                uri = skill.get("uri", "")
                abstract = (skill.get("abstract") or "").strip()
                lines.append(f"{idx}. {uri}")
                if abstract:
                    lines.append(f"   Abstract: {abstract}")

        if concrete_document_resources:
            lines.append("")
            lines.append(
                "Important: search results are retrieval metadata only. Before answering, call "
                "openviking_read on the most relevant document URI."
            )
            if image_resources:
                lines.append(
                    "If the reply should include document illustrations, screenshots, or bid "
                    "attachments, you can either read the document with include_images=true "
                    "or read a matched image asset URI directly."
                )
                lines.append(
                    "Never place raw viking:// image URIs directly inside Markdown image syntax."
                )
        elif summary_resources:
            lines.append("")
            if any(cls._is_generic_scope_summary_uri(resource.get("uri", "")) for resource in summary_resources):
                lines.append(
                    "Important: only summary matches were found, including generic scope summaries. "
                    "They are not concrete document evidence and should not be used directly for answering."
                )
            else:
                lines.append(
                    "Important: only summary matches were found. They are not concrete "
                    "document evidence and should not be used directly for answering."
                )
            lines.append(
                "Next step: use openviking_glob to locate concrete files under the target URI, "
                "or narrow target_uri and search again."
            )
            lines.append(
                "After finding a concrete document URI, call openviking_read on that URI."
            )
        elif image_resources:
            lines.append("")
            lines.append(
                "Important: search results are retrieval metadata only. Before answering, call "
                "openviking_read on the most relevant image URI shown above."
            )
            lines.append(
                "For image resources, openviking_read(level='read', include_images=true) returns "
                "sendable Markdown image lines directly."
            )

        return "\n".join(lines)


class VikingAddResourceTool(OVFileTool):
    """Tool to add a resource to Viking."""

    @property
    def name(self) -> str:
        return "openviking_add_resource"

    @property
    def description(self) -> str:
        return "Add a resource (url like pic, git code or local file path) to OpenViking.This is a asynchronous operation."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Url or local file path"},
                "description": {"type": "string", "description": "Description of the resource"},
            },
            "required": ["path", "description"],
        }

    async def execute(
        self,
        tool_context: "ToolContext",
        path: str,
        description: str,
        **kwargs: Any,
    ) -> str:
        client = None
        try:
            if path and not path.startswith("http"):
                local_path = Path(path).expanduser().resolve()
                if not local_path.exists():
                    return f"Error: File not found: {path}"
                if not local_path.is_file():
                    return f"Error: Not a file: {path}"

            client = await VikingClient.create(tool_context.workspace_id)
            result = await client.add_resource(path, description)

            if result:
                root_uri = result.get("root_uri", "")
                return f"Successfully added resource: {root_uri}"
            else:
                return "Failed to add resource"
        except httpx.ReadTimeout:
            return f"Request timed out. The resource addition task may still be processing on the server side."
        except Exception as e:
            logger.warning(f"Error adding resource: {e}")
            return f"Error adding resource to Viking: {str(e)}"
        finally:
            if client:
                await client.close()


class VikingGrepTool(OVFileTool):
    """Tool to search Viking resources using regex patterns."""

    @property
    def name(self) -> str:
        return "openviking_grep"

    @property
    def description(self) -> str:
        return ("Search Viking resources using regex patterns (like grep). Supports multiple patterns to search concurrently."
                "Please avoid repeated calls with similar queries as much as possible.")

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uri": {
                    "type": "string",
                    "description": "The whole Viking URI to search within (e.g., viking://resources/)",
                },
                "pattern": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Regex pattern or array of regex patterns to search for",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive search",
                    "default": False,
                },
            },
            "required": ["uri", "pattern"],
        }

    async def execute(
        self,
        tool_context: "ToolContext",
        uri: str,
        pattern: Union[str, list[str]],
        case_insensitive: bool = False,
        **kwargs: Any,
    ) -> str:
        try:
            client = await self._get_client(tool_context)
            patterns = [pattern] if isinstance(pattern, str) else pattern

            # Limit concurrent requests to avoid overwhelming the server and memory
            max_concurrent = 10
            semaphore = asyncio.Semaphore(max_concurrent)

            async def run_grep(p: str) -> tuple[str, list[Any]]:
                async with semaphore:
                    try:
                        result = await client.grep(uri, p, case_insensitive=case_insensitive)
                        if isinstance(result, dict):
                            matches = result.get("matches", [])
                        else:
                            matches = getattr(result, "matches", [])
                        return (p, matches)
                    except Exception as e:
                        logger.warning(f"Error searching for pattern '{p}': {e}")
                        return (p, [])

            tasks = [run_grep(p) for p in patterns]
            results = await asyncio.gather(*tasks)

            # Merge results by URI
            merged_results: dict[str, list[tuple[int, str, str]]] = {}
            total_matches = 0

            for p, matches in results:
                if not matches:
                    continue
                total_matches += len(matches)
                for match in matches:
                    if isinstance(match, dict):
                        match_uri = match.get("uri", "unknown")
                        line = match.get("line", "?")
                        content = match.get("content", "")
                    else:
                        match_uri = getattr(match, "uri", "unknown")
                        line = getattr(match, "line", "?")
                        content = getattr(match, "content", "")

                    if match_uri not in merged_results:
                        merged_results[match_uri] = []
                    merged_results[match_uri].append((line, content, p))

            if not merged_results:
                pattern_str = ", ".join(f"'{p}'" for p in patterns)
                return f"No matches found for patterns: {pattern_str}"

            # Format output
            result_lines = [f"Found {total_matches} match{'es' if total_matches != 1 else ''} across {len(patterns)} pattern{'s' if len(patterns) != 1 else ''}:"]

            for match_uri, matches in merged_results.items():
                # Sort matches by line number
                matches.sort(key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0)
                result_lines.append(f"\n📄 {match_uri}")
                for line, content, pattern_name in matches:
                    result_lines.append(f"   Line {line} (pattern: '{pattern_name}'):")
                    result_lines.append(f"   {content}")

            return "\n".join(result_lines)
        except Exception as e:
            return f"Error searching Viking with grep: {str(e)}"


class VikingGlobTool(OVFileTool):
    """Tool to find Viking resources using glob patterns."""

    @property
    def name(self) -> str:
        return "openviking_glob"

    @property
    def description(self) -> str:
        return "Find Viking resources using glob patterns (like **/*.md, *.py)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern to match (e.g., **/*.md, *.py, src/**/*.js)",
                },
                "uri": {
                    "type": "string",
                    "description": "The whole Viking URI to search within (e.g., viking://resources/path/)",
                    "default": "",
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self, tool_context: "ToolContext", pattern: str, uri: str = "", **kwargs: Any
    ) -> str:
        try:
            client = await self._get_client(tool_context)
            result = await client.glob(pattern, uri=uri or None)

            if isinstance(result, dict):
                matches = result.get("matches", [])
                count = result.get("count", 0)
            else:
                matches = getattr(result, "matches", [])
                count = getattr(result, "count", 0)

            if not matches:
                return f"No files found for pattern: {pattern}"

            result_lines = [f"Found {count} file{'s' if count != 1 else ''}:"]
            for match_uri in matches:
                if isinstance(match_uri, dict):
                    match_uri = match_uri.get("uri", str(match_uri))
                result_lines.append(f"📄 {match_uri}")

            return "\n".join(result_lines)
        except Exception as e:
            return f"Error searching Viking with glob: {str(e)}"

class VikingMemoryCommitTool(OVFileTool):
    """Tool to commit messages to OpenViking session."""

    @property
    def name(self) -> str:
        return "openviking_memory_commit"

    @property
    def description(self) -> str:
        return "When user has personal information needs to be remembered, Commit messages to OpenViking."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "description": "List of messages to commit, each with role, content",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": ["user", "assistant"]},
                            "content": {"type": "string"},
                        },
                        "required": ["role", "content"],
                    },
                },
            },
            "required": ["messages"],
        }

    async def execute(
        self,
        tool_context: ToolContext,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        try:
            if not tool_context.sender_id:
                return "Error committed, sender_id is required."
            client = await self._get_client(tool_context)
            session_id = tool_context.session_key.safe_name()
            await client.commit(session_id, messages, tool_context.sender_id)
            return f"Successfully committed to session {session_id}"
        except Exception as e:
            logger.exception(f"Error processing message: {e}")
            return f"Error committing to Viking: {str(e)}"

class VikingMultiReadTool(OVFileTool):
    """Tool to read content from multiple Viking resources concurrently."""

    @property
    def name(self) -> str:
        return "openviking_multi_read"

    @property
    def description(self) -> str:
        return "Read full content from multiple OpenViking resources concurrently. Returns complete content for all URIs with no truncation."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "uris": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of Viking file URIs to read from (e.g., [\"viking://resources/path/123.md\", \"viking://resources/path/456.md\"])",
                },
            },
            "required": ["uris"],
        }

    async def execute(
        self,
        tool_context: ToolContext,
        uris: list[str],
        **kwargs: Any,
    ) -> str:
        level = "read"  # 默认获取完整内容
        try:
            if not uris:
                return "Error: No URIs provided."

            client = await self._get_client(tool_context)
            max_concurrent = 10
            semaphore = asyncio.Semaphore(max_concurrent)

            async def read_single_uri(uri: str) -> dict:
                async with semaphore:
                    try:
                        content = await client.read_content(uri, level=level)
                        return {
                            "uri": uri,
                            "content": content,
                            "success": True,
                        }
                    except Exception as e:
                        logger.warning(f"Error reading from {uri}: {e}")
                        return {
                            "uri": uri,
                            "content": f"Error reading from Viking: {str(e)}",
                            "success": False,
                        }

            # 并发读取所有URI
            read_tasks = [read_single_uri(uri) for uri in uris]
            results = await asyncio.gather(*read_tasks)

            # 构建结果
            result_lines = [f"Multi-read results for {len(uris)} resources (level: {level}):"]

            for i, result in enumerate(results, 1):
                uri = result["uri"]
                content = result["content"]
                success = result["success"]

                result_lines.append(f"\n--- START OF {uri} ---")
                if success:
                    result_lines.append(content)
                else:
                    result_lines.append(f"ERROR: {content}")
                result_lines.append(f"--- END OF {uri} ---")

            return "\n".join(result_lines)

        except Exception as e:
            logger.exception(f"Error in VikingMultiReadTool: {e}")
            return f"Error multi-reading Viking resources: {str(e)}"
