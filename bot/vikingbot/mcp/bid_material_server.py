"""Minimal stdio MCP server for bid-material retrieval."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Any, BinaryIO

from vikingbot.config.schema import Config
from vikingbot.services.bid_material import BidMaterialService

MCP_PROTOCOL_VERSION = "2024-11-05"


@dataclass(frozen=True)
class _MCPToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


class BidMaterialMCPServer:
    """Small MCP server exposing three high-level bid-material tools."""

    def __init__(self, config: Config | None = None, service: BidMaterialService | None = None) -> None:
        self.config = config or Config()
        self.service = service or BidMaterialService(agent_id=self.config.ov_server.agent_id or "bid-material-mcp")
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
                            "default": "viking://resources/",
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
                            "default": "viking://resources/",
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
                            "default": "viking://resources/",
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
                target_uri=str(arguments.get("target_uri") or "viking://resources/"),
                top_k=int(arguments.get("top_k", 5)),
            )
            return self.service.pack_to_json(pack)

        if name == "search_solution_materials":
            pack = await self.service.search_solution_materials(
                query=str(arguments["query"]),
                target_uri=str(arguments.get("target_uri") or "viking://resources/"),
                top_k=int(arguments.get("top_k", 5)),
            )
            return self.service.pack_to_json(pack)

        if name == "collect_bid_evidence":
            pack = await self.service.collect_bid_evidence(
                section_name=str(arguments["section_name"]),
                requirement=str(arguments["requirement"]),
                target_uri=str(arguments.get("target_uri") or "viking://resources/"),
                top_k=int(arguments.get("top_k", 8)),
            )
            return self.service.pack_to_json(pack)

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
