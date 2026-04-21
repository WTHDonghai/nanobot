# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the bid-material MCP server."""

import io
import json

import pytest

from vikingbot.config.schema import Config
from vikingbot.mcp.bid_material_server import BidMaterialMCPServer
from vikingbot.services.bid_material import EvidencePack


class FakeBidMaterialService:
    async def search_certificates(self, **kwargs):
        return EvidencePack(
            intent="certificate",
            query=kwargs["query"],
            target_uri=kwargs["target_uri"],
            summary="ok",
            items=[],
            gaps=["none"],
        )

    async def search_solution_materials(self, **kwargs):
        return EvidencePack(
            intent="solution",
            query=kwargs["query"],
            target_uri=kwargs["target_uri"],
            summary="ok",
            items=[],
            gaps=[],
        )

    async def collect_bid_evidence(self, **kwargs):
        return EvidencePack(
            intent="section_evidence",
            query=f'{kwargs["section_name"]}: {kwargs["requirement"]}',
            target_uri=kwargs["target_uri"],
            summary="ok",
            items=[],
            gaps=[],
            claims=[],
        )

    def pack_to_json(self, pack: EvidencePack) -> str:
        payload = {
            "intent": pack.intent,
            "query": pack.query,
            "target_uri": pack.target_uri,
            "summary": pack.summary,
            "items": [],
        }
        if pack.claims is not None:
            payload["claims"] = [claim.model_dump(mode="json") for claim in pack.claims]
        return json.dumps(payload, ensure_ascii=False)

    async def read_resource_markdown(self, **kwargs) -> str:
        return f'# Resource\n\nSource: {kwargs["uri"]}\n\n![image](/tmp/cert.png)'


@pytest.mark.asyncio
async def test_mcp_initialize_and_tools_list() -> None:
    server = BidMaterialMCPServer(config=Config(), service=FakeBidMaterialService())

    initialize = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    tools = await server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )

    assert initialize["result"]["capabilities"]["tools"]["listChanged"] is False
    assert initialize["result"]["capabilities"]["resources"]["listChanged"] is False
    assert {tool["name"] for tool in tools["result"]["tools"]} == {
        "search_certificates",
        "search_solution_materials",
        "collect_bid_evidence",
    }
    solution_tool = next(
        tool for tool in tools["result"]["tools"] if tool["name"] == "search_solution_materials"
    )
    assert "max_images_per_item" not in solution_tool["inputSchema"]["properties"]


@pytest.mark.asyncio
async def test_mcp_tool_call_returns_text_payload() -> None:
    server = BidMaterialMCPServer(config=Config(), service=FakeBidMaterialService())

    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "search_certificates",
                "arguments": {"query": "营业执照", "target_uri": "viking://resources/"},
            },
        }
    )

    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["intent"] == "certificate"
    assert "gaps" not in payload


@pytest.mark.asyncio
async def test_mcp_resources_read_returns_markdown_payload() -> None:
    server = BidMaterialMCPServer(config=Config(), service=FakeBidMaterialService())

    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "resources/read",
            "params": {"uri": "viking://resources/demo/cert.md"},
        }
    )

    contents = response["result"]["contents"]
    assert contents[0]["uri"] == "viking://resources/demo/cert.md"
    assert contents[0]["mimeType"] == "text/markdown"
    assert "![image](/tmp/cert.png)" in contents[0]["text"]


def test_stdio_message_helpers_support_line_and_framed_protocols() -> None:
    line_message, line_mode = BidMaterialMCPServer._read_stdio_message(
        io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\n')
    )
    assert line_mode == "line"
    assert line_message["method"] == "ping"

    framed_body = b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{}}'
    framed_input = io.BytesIO(
        f"Content-Length: {len(framed_body)}\r\n\r\n".encode("ascii") + framed_body
    )
    framed_message, framed_mode = BidMaterialMCPServer._read_stdio_message(framed_input)
    assert framed_mode == "framed"
    assert framed_message["id"] == 2

    line_output = io.BytesIO()
    BidMaterialMCPServer._write_stdio_message(
        line_output,
        {"jsonrpc": "2.0", "id": 3, "result": {}},
        "line",
    )
    assert json.loads(line_output.getvalue().decode("utf-8"))["id"] == 3

    framed_output = io.BytesIO()
    BidMaterialMCPServer._write_stdio_message(
        framed_output,
        {"jsonrpc": "2.0", "id": 4, "result": {}},
        "framed",
    )
    assert framed_output.getvalue().startswith(b"Content-Length: ")
