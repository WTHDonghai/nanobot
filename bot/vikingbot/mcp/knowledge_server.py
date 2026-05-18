"""Minimal stdio MCP server for OpenViking knowledge retrieval."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any, BinaryIO

from vikingbot.config.schema import Config
from vikingbot.services.openviking_explorer import DEFAULT_TARGET_URI, OpenVikingExplorerService

MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True)
class MCPToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


class KnowledgeMCPServer:
    """Small MCP server exposing domain-neutral OpenViking knowledge tools."""

    server_name = "vikingbot-knowledge-base"
    resource_name = "knowledge-root"
    resource_description = "OpenViking knowledge-base resource root."
    default_agent_id = "knowledge-mcp"

    def __init__(
        self,
        config: Config | None = None,
        explorer: OpenVikingExplorerService | None = None,
    ) -> None:
        self.config = config or Config()
        self._agent_id = self.config.ov_server.agent_id or self.default_agent_id
        self.explorer = explorer or OpenVikingExplorerService(agent_id=self._agent_id)
        self._tools = self._build_tools()

    def _build_tools(self) -> dict[str, MCPToolDefinition]:
        return {
            "openviking_search": MCPToolDefinition(
                name="openviking_search",
                description="Search OpenViking resources and return candidate document, image, and summary URIs. Use this to locate relevant knowledge-base material before drafting.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Knowledge-base search query"},
                        "target_uri": {
                            "type": "string",
                            "description": "Optional search scope",
                            "default": DEFAULT_TARGET_URI,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            "openviking_list": MCPToolDefinition(
                name="openviking_list",
                description="List files and folders under one OpenViking URI so clients can inspect what knowledge-base materials exist in the current scope.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "uri": {
                            "type": "string",
                            "description": "Folder URI to inspect",
                            "default": DEFAULT_TARGET_URI,
                        },
                        "recursive": {
                            "type": "boolean",
                            "description": "Whether to list recursively",
                            "default": False,
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            "openviking_glob": MCPToolDefinition(
                name="openviking_glob",
                description="Find concrete files with a glob pattern under one OpenViking scope. Use this to narrow from folders or summary directories to exact material files.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern, for example **/*.md or **/*架构*",
                        },
                        "uri": {
                            "type": "string",
                            "description": "Optional search scope",
                            "default": DEFAULT_TARGET_URI,
                        },
                    },
                    "required": ["pattern"],
                    "additionalProperties": False,
                },
            ),
            "openviking_read": MCPToolDefinition(
                name="openviking_read",
                description="Read one OpenViking document, image, or directory target. Returns markdown with local image paths for MCP clients.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "uri": {"type": "string", "description": "Concrete resource URI to read"},
                        "level": {
                            "type": "string",
                            "description": "Reading level. Use read by default; abstract/overview are only for deliberate summarization.",
                            "enum": ["abstract", "overview", "read"],
                            "default": "read",
                        },
                        "include_images": {
                            "type": "boolean",
                            "description": "When level=read, materialize inline or nearby images into local markdown links.",
                            "default": True,
                        },
                        "max_images": {
                            "type": "integer",
                            "description": "Maximum number of fallback nearby images to append when inline image anchors are absent.",
                            "default": 8,
                        },
                    },
                    "required": ["uri"],
                    "additionalProperties": False,
                },
            ),
            "collect_evidence": MCPToolDefinition(
                name="collect_evidence",
                description="Collect a small domain-neutral evidence pack by searching the knowledge base and reading the top concrete documents. Use this before writing answers that need documentary support.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Evidence collection query"},
                        "target_uri": {
                            "type": "string",
                            "description": "Optional search scope",
                            "default": DEFAULT_TARGET_URI,
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Maximum number of concrete documents to read",
                            "default": 3,
                        },
                        "include_images": {
                            "type": "boolean",
                            "description": "Whether to include materialized image links while reading documents",
                            "default": True,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
        }

    async def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one MCP JSON-RPC request."""
        method = message.get("method")
        if not method:
            return None

        request_id = message.get("id")
        params = message.get("params") or {}

        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return self._response(
                request_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "serverInfo": {"name": self.server_name, "version": "0.1.0"},
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                        "prompts": {"listChanged": False},
                    },
                },
            )
        if method == "ping":
            return self._response(request_id, {})
        if method == "resources/list":
            return self._response(
                request_id,
                {
                    "resources": [
                        {
                            "uri": "viking://resources/",
                            "name": self.resource_name,
                            "description": self.resource_description,
                            "mimeType": "text/markdown",
                        }
                    ]
                },
            )
        if method == "resources/read":
            try:
                uri = str(params.get("uri") or "").strip()
                if not uri:
                    return self._error(request_id, code=-32602, message="Missing resource uri")
                payload = await self.explorer.openviking_read(
                    uri=uri,
                    level="read",
                    include_images=True,
                    max_images=8,
                )
                text = self._render_resource_read_text(payload)
                return self._response(
                    request_id,
                    {
                        "contents": [
                            {
                                "uri": uri,
                                "mimeType": "text/markdown",
                                "text": text,
                            }
                        ]
                    },
                )
            except Exception as exc:
                return self._error(request_id, code=-32000, message=str(exc))
        if method == "prompts/list":
            return self._response(request_id, {"prompts": []})
        if method == "prompts/get":
            return self._error(request_id, code=-32602, message="Prompt not found")
        if method == "tools/list":
            return self._response(
                request_id,
                {
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": tool.input_schema,
                        }
                        for tool in self._tools.values()
                    ]
                },
            )
        if method == "tools/call":
            try:
                result = await self._call_tool(
                    name=str(params.get("name") or ""),
                    arguments=params.get("arguments") or {},
                )
                return self._response(
                    request_id,
                    {"content": [{"type": "text", "text": result}], "isError": False},
                )
            except Exception as exc:
                return self._response(
                    request_id,
                    {
                        "content": [{"type": "text", "text": str(exc)}],
                        "isError": True,
                    },
                )

        return self._error(request_id, code=-32601, message=f"Method not found: {method}")

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self._tools:
            raise ValueError(f"Unknown tool: {name}")

        if name == "openviking_search":
            payload = await self.explorer.openviking_search(
                query=str(arguments["query"]),
                target_uri=str(arguments.get("target_uri") or DEFAULT_TARGET_URI),
            )
            return json.dumps(payload, ensure_ascii=False)

        if name == "openviking_list":
            payload = await self.explorer.openviking_list(
                uri=str(arguments.get("uri") or DEFAULT_TARGET_URI),
                recursive=bool(arguments.get("recursive", False)),
            )
            return json.dumps(payload, ensure_ascii=False)

        if name == "openviking_glob":
            payload = await self.explorer.openviking_glob(
                pattern=str(arguments["pattern"]),
                uri=str(arguments.get("uri") or DEFAULT_TARGET_URI),
            )
            return json.dumps(payload, ensure_ascii=False)

        if name == "openviking_read":
            payload = await self.explorer.openviking_read(
                uri=str(arguments["uri"]),
                level=str(arguments.get("level") or "read"),
                include_images=bool(arguments.get("include_images", True)),
                max_images=int(arguments.get("max_images", 8)),
            )
            return json.dumps(payload, ensure_ascii=False)

        if name == "collect_evidence":
            payload = await self._collect_evidence(
                query=str(arguments["query"]),
                target_uri=str(arguments.get("target_uri") or DEFAULT_TARGET_URI),
                top_k=int(arguments.get("top_k", 3)),
                include_images=bool(arguments.get("include_images", True)),
            )
            return json.dumps(payload, ensure_ascii=False)

        raise ValueError(f"Unknown tool: {name}")

    async def _collect_evidence(
        self,
        *,
        query: str,
        target_uri: str = DEFAULT_TARGET_URI,
        top_k: int = 3,
        include_images: bool = True,
    ) -> dict[str, Any]:
        search_payload = await self.explorer.openviking_search(
            query=query,
            target_uri=target_uri,
        )
        documents = search_payload.get("documents") or []
        items: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []

        for document in documents[: max(top_k, 0)]:
            uri = str(document.get("uri") or "").strip()
            if not uri:
                continue
            read_payload = await self.explorer.openviking_read(
                uri=uri,
                level="read",
                include_images=include_images,
                max_images=4,
            )
            kind = str(read_payload.get("kind") or document.get("kind") or "document")
            content_markdown = str(read_payload.get("content_markdown") or "").strip()
            if self._is_navigation_or_empty_read(kind, content_markdown):
                gaps.append(
                    {
                        "uri": uri,
                        "kind": kind,
                        "note": read_payload.get("note", ""),
                        "candidate_uris": read_payload.get("candidate_uris", []),
                    }
                )
                continue
            items.append(
                {
                    "uri": uri,
                    "kind": kind,
                    "score": document.get("score", 0.0),
                    "match_reason": document.get("match_reason", ""),
                    "note": read_payload.get("note", ""),
                    "candidate_uris": read_payload.get("candidate_uris", []),
                    "content_markdown": content_markdown,
                }
            )

        if not items and not gaps:
            gaps.append(
                {
                    "uri": "",
                    "kind": "empty_result",
                    "note": "No concrete document evidence was collected from the current scope.",
                    "candidate_uris": [],
                }
            )

        return {
            "query": query,
            "target_uri": target_uri,
            "summary": (
                f"Collected {len(items)} concrete evidence item(s)."
                if items
                else "No concrete evidence was collected."
            ),
            "items": items,
            "gaps": gaps,
            "search": {
                "total": search_payload.get("total", 0),
                "next_step": search_payload.get("next_step", ""),
                "summaries": search_payload.get("summaries", []),
                "images": search_payload.get("images", []),
            },
        }

    @staticmethod
    def _is_navigation_or_empty_read(kind: str, content_markdown: str) -> bool:
        if kind in {"missing", "directory", "summary", "scope_summary"}:
            return True
        return not content_markdown

    @staticmethod
    def _render_resource_read_text(payload: dict[str, Any]) -> str:
        content = str(payload.get("content_markdown") or "").strip()
        if content:
            return content

        note = str(payload.get("note") or "No readable content was returned.").strip()
        candidate_uris = [
            str(uri).strip()
            for uri in (payload.get("candidate_uris") or [])
            if str(uri).strip()
        ]
        if not candidate_uris:
            return note

        candidates = "\n".join(f"- {uri}" for uri in candidate_uris)
        return f"{note}\n\nCandidate URIs:\n{candidates}"

    async def run_stdio(
        self,
        input_stream: BinaryIO | None = None,
        output_stream: BinaryIO | None = None,
    ) -> None:
        """Run the MCP server over stdio.

        Some MCP clients use newline-delimited JSON-RPC over stdio, while
        our local debug script uses Content-Length framing.
        Accept both input formats and reply using the same format we received.
        """
        input_stream = input_stream or sys.stdin.buffer
        output_stream = output_stream or sys.stdout.buffer

        while True:
            message, mode = await asyncio.to_thread(self._read_stdio_message, input_stream)
            if message is None:
                break
            response = await self.handle_message(message)
            if response is None:
                continue
            await asyncio.to_thread(self._write_stdio_message, output_stream, response, mode or "line")

    @staticmethod
    def _read_stdio_message(stream: BinaryIO) -> tuple[dict[str, Any] | None, str | None]:
        while True:
            line = stream.readline()
            if not line:
                return None, None
            if line in {b"\r\n", b"\n"}:
                continue
            stripped = line.strip()
            if not stripped:
                continue

            # Official MCP SDK stdio transport is line-delimited JSON-RPC.
            if stripped.startswith((b"{", b"[")):
                return json.loads(stripped.decode("utf-8")), "line"

            # Keep compatibility with Content-Length framed local tests.
            if b":" not in line:
                continue

            headers: dict[str, str] = {}
            header = line.decode("utf-8").strip()
            key, value = header.split(":", 1)
            headers[key.strip().lower()] = value.strip()

            while True:
                line = stream.readline()
                if not line:
                    return None, "framed"
                if line in {b"\r\n", b"\n"}:
                    break
                header = line.decode("utf-8").strip()
                if ":" not in header:
                    continue
                key, value = header.split(":", 1)
                headers[key.strip().lower()] = value.strip()

            content_length = int(headers.get("content-length", "0"))
            if content_length <= 0:
                return None, "framed"
            body = stream.read(content_length)
            if not body:
                return None, "framed"
            return json.loads(body.decode("utf-8")), "framed"

    @staticmethod
    def _write_stdio_message(stream: BinaryIO, payload: dict[str, Any], mode: str) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if mode == "framed":
            header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
            stream.write(header)
            stream.write(body)
        else:
            stream.write(body)
            stream.write(b"\n")
        stream.flush()

    @staticmethod
    def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, *, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
