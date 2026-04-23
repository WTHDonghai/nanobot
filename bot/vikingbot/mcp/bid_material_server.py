"""Minimal stdio MCP server for bid-material retrieval."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any, BinaryIO

from vikingbot.config.schema import Config
from vikingbot.services.bid_material import BidMaterialService
from vikingbot.services.openviking_explorer import DEFAULT_TARGET_URI, OpenVikingExplorerService

MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True)
class _MCPToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


class BidMaterialMCPServer:
    """Small MCP server exposing three high-level bid-material tools."""

    def __init__(
        self,
        config: Config | None = None,
        service: BidMaterialService | None = None,
        explorer: OpenVikingExplorerService | None = None,
    ) -> None:
        self.config = config or Config()
        agent_id = self.config.ov_server.agent_id or "bid-material-mcp"
        self.service = service or BidMaterialService(agent_id=agent_id)
        self.explorer = explorer or OpenVikingExplorerService(agent_id=agent_id)
        self._tools = {
            "search_certificates": _MCPToolDefinition(
                name="search_certificates",
                description="Search qualification certificates, licenses, and authorization materials. Use this before drafting qualification sections. Returns markdown-ready evidence snippets with inline images.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Certificate lookup query"},
                        "target_uri": {
                            "type": "string",
                            "description": "Optional search scope",
                            "default": DEFAULT_TARGET_URI,
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Maximum number of evidence items",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            "search_solution_materials": _MCPToolDefinition(
                name="search_solution_materials",
                description="Search solution, product, case-study, and parameter materials. Read returned source URIs before drafting long-form prose. Returns markdown-ready evidence snippets with inline images.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Solution material query"},
                        "target_uri": {
                            "type": "string",
                            "description": "Optional search scope",
                            "default": DEFAULT_TARGET_URI,
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Maximum number of evidence items",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            "collect_bid_evidence": _MCPToolDefinition(
                name="collect_bid_evidence",
                description="Collect reusable evidence for one bid-response section. Do not draft the final section from memory before using this evidence. Each item contains markdown-ready evidence content.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "section_name": {"type": "string", "description": "Bid section name"},
                        "requirement": {"type": "string", "description": "Requirement or section brief"},
                        "target_uri": {
                            "type": "string",
                            "description": "Optional search scope",
                            "default": DEFAULT_TARGET_URI,
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Maximum number of merged evidence items",
                            "default": 8,
                        },
                    },
                    "required": ["section_name", "requirement"],
                    "additionalProperties": False,
                },
            ),
            "openviking_search": _MCPToolDefinition(
                name="openviking_search",
                description="Search OpenViking resources and return candidate document, image, and summary URIs. Use this when high-level bid-material retrieval is too shallow and Hermes needs to inspect the underlying material filesystem.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Filesystem search query"},
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
            "openviking_list": _MCPToolDefinition(
                name="openviking_list",
                description="List files and folders under one OpenViking URI so Hermes can inspect what bidding materials exist in the current scope.",
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
            "openviking_glob": _MCPToolDefinition(
                name="openviking_glob",
                description="Find concrete files with a glob pattern under one OpenViking scope. Use this to narrow from chapter folders or summary directories to exact material files.",
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
            "openviking_read": _MCPToolDefinition(
                name="openviking_read",
                description="Read one OpenViking document, image, or directory target. Returns markdown with local image paths for Hermes instead of send:// channel images.",
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
                    "serverInfo": {"name": "vikingbot-bid-material", "version": "0.1.0"},
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
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
                            "name": "bid-material-root",
                            "description": "OpenViking bid-material resource root.",
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
                text = await self.service.read_resource_markdown(uri=uri)
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
        if name == "search_certificates":
            pack = await self.service.search_certificates(
                query=str(arguments["query"]),
                target_uri=str(arguments.get("target_uri") or DEFAULT_TARGET_URI),
                top_k=int(arguments.get("top_k", 5)),
            )
            return self.service.pack_to_json(pack)

        if name == "search_solution_materials":
            pack = await self.service.search_solution_materials(
                query=str(arguments["query"]),
                target_uri=str(arguments.get("target_uri") or DEFAULT_TARGET_URI),
                top_k=int(arguments.get("top_k", 5)),
            )
            return self.service.pack_to_json(pack)

        if name == "collect_bid_evidence":
            pack = await self.service.collect_bid_evidence(
                section_name=str(arguments["section_name"]),
                requirement=str(arguments["requirement"]),
                target_uri=str(arguments.get("target_uri") or DEFAULT_TARGET_URI),
                top_k=int(arguments.get("top_k", 8)),
            )
            return self.service.pack_to_json(pack)

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

        raise ValueError(f"Unknown tool: {name}")

    async def run_stdio(
        self,
        input_stream: BinaryIO | None = None,
        output_stream: BinaryIO | None = None,
    ) -> None:
        """Run the MCP server over stdio.

        Hermes's native MCP client currently uses newline-delimited JSON-RPC
        over stdio, while our local debug script uses Content-Length framing.
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
