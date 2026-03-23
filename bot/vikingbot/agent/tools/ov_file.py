"""OpenViking file system tools: read, write, list, search resources."""

from abc import ABC
from pathlib import Path
from typing import Any, Optional

import httpx
from loguru import logger

from vikingbot.agent.tools.base import Tool, ToolContext
from vikingbot.openviking_mount.ov_server import VikingClient


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
                return await client.read_content(uri, level=level)

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

            content = await client.materialize_inline_image_refs(content, read_uri)
            if "send://" in content:
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


class VikingListTool(OVFileTool):
    """Tool to list Viking resources."""

    @property
    def name(self) -> str:
        return "openviking_list"

    @property
    def description(self) -> str:
        return "List resources in a OpenViking path."

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
            "/_images/ are auxiliary screenshots, not the main answer source."
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
            results = await client.search(query, target_uri=target_uri)

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

    @classmethod
    def _format_search_results(
        cls, query: str, results: dict[str, Any], target_uri: Optional[str] = ""
    ) -> str:
        resources = results.get("resources") or []
        memories = results.get("memories") or []
        skills = results.get("skills") or []
        ordered_resources = [
            resource
            for _, resource in sorted(
                enumerate(resources),
                key=lambda item: (cls._is_image_uri(item[1].get("uri", "")), item[0]),
            )
        ]
        document_resources = [
            resource for resource in ordered_resources if not cls._is_image_uri(resource.get("uri", ""))
        ]
        image_resources = [
            resource for resource in ordered_resources if cls._is_image_uri(resource.get("uri", ""))
        ]
        display_resources = document_resources if document_resources else ordered_resources

        lines = [f"OpenViking search query: {query}"]
        if target_uri:
            lines.append(f"Target URI: {target_uri}")
        lines.append(f"Total matches: {results.get('total', len(resources))}")

        if display_resources:
            lines.append("")
            lines.append("Resources:")
            for idx, resource in enumerate(display_resources, start=1):
                uri = resource.get("uri", "")
                match_reason = (resource.get("match_reason") or "").strip()
                resource_type = "image asset" if cls._is_image_uri(uri) else "document"

                lines.append(f"{idx}. [{resource_type}] {uri}")
                if match_reason:
                    lines.append(f"   Match reason: {match_reason}")
                lines.append("   Content preview omitted. Use openviking_read for evidence.")

        if document_resources and image_resources:
            lines.append("")
            lines.append(
                f"Related image assets: {len(image_resources)} matched screenshot(s) were found, "
                "but their raw Viking URIs are intentionally omitted here."
            )
            lines.append(
                "To send images to the user, call openviking_read(level='read', include_images=true) "
                "on the matched document URI, and only use the returned Markdown image lines."
            )

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

        if document_resources:
            lines.append("")
            lines.append(
                "Important: search results are retrieval metadata only. Before answering, call "
                "openviking_read on the most relevant document URI."
            )
            if image_resources:
                lines.append(
                    "If the reply should include screenshots, use "
                    "openviking_read(level='read', include_images=true) on that document URI "
                    "and only keep the returned Markdown image lines unchanged."
                )
                lines.append(
                    "Never place raw viking:// image URIs directly inside Markdown image syntax."
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
        return "Search Viking resources using regex patterns (like grep)."

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
                    "type": "string",
                    "description": "Regex pattern to search for",
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
        pattern: str,
        case_insensitive: bool = False,
        **kwargs: Any,
    ) -> str:
        try:
            client = await self._get_client(tool_context)
            result = await client.grep(uri, pattern, case_insensitive=case_insensitive)

            if isinstance(result, dict):
                matches = result.get("matches", [])
                count = result.get("count", 0)
            else:
                matches = getattr(result, "matches", [])
                count = getattr(result, "count", 0)

            if not matches:
                return f"No matches found for pattern: {pattern}"

            result_lines = [f"Found {count} match{'es' if count != 1 else ''}:"]
            for match in matches:
                if isinstance(match, dict):
                    match_uri = match.get("uri", "unknown")
                    line = match.get("line", "?")
                    content = match.get("content", "")
                else:
                    match_uri = getattr(match, "uri", "unknown")
                    line = getattr(match, "line", "?")
                    content = getattr(match, "content", "")
                result_lines.append(f"\n📄 {match_uri}:{line}")
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


class VikingSearchUserMemoryTool(OVFileTool):
    """Tool to search Viking user memories"""

    @property
    def name(self) -> str:
        return "user_memory_search"

    @property
    def description(self) -> str:
        return "Search for user memories in OpenViking using a query."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "The search query"}},
            "required": ["query"],
        }

    async def execute(self, tool_context: ToolContext, query: str, **kwargs: Any) -> str:
        try:
            client = await self._get_client(tool_context)
            results = await client.search_user_memory(query, tool_context.sender_id)

            if not results:
                return f"No results found for query: {query}"
            return str(results)
        except Exception as e:
            return f"Error searching Viking: {str(e)}"


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
