# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for the bid-material MCP server."""

import io
import json

import pytest

from vikingbot.config.schema import CapabilityProfile, Config
from vikingbot.mcp.bid_material_server import BidMaterialMCPServer
from vikingbot.mcp.knowledge_server import KnowledgeMCPServer
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


class FakeOpenVikingExplorerService:
    async def openviking_search(self, **kwargs):
        return {
            "query": kwargs["query"],
            "target_uri": kwargs["target_uri"],
            "total": 1,
            "documents": [
                {
                    "uri": "viking://resources/demo/doc.md",
                    "kind": "document",
                    "score": 0.9,
                    "match_reason": "matched",
                    "abstract": "demo abstract",
                    "category": "",
                    "context_type": "",
                }
            ],
            "images": [],
            "summaries": [],
            "next_step": "Read the most relevant concrete document URI with openviking_read before drafting.",
        }

    async def openviking_list(self, **kwargs):
        return {
            "uri": kwargs["uri"],
            "recursive": kwargs["recursive"],
            "count": 1,
            "entries": [
                {
                    "name": "demo",
                    "uri": "viking://resources/demo",
                    "is_dir": True,
                    "size": 0,
                }
            ],
        }

    async def openviking_glob(self, **kwargs):
        return {
            "pattern": kwargs["pattern"],
            "uri": kwargs["uri"],
            "count": 1,
            "matches": ["viking://resources/demo/doc.md"],
        }

    async def openviking_read(self, **kwargs):
        return {
            "uri": kwargs["uri"],
            "level": kwargs["level"],
            "kind": "document",
            "resolved_uri": kwargs["uri"],
            "note": "",
            "candidate_uris": [],
            "image_count": 1,
            "content_markdown": "正文\n\n![架构图](/tmp/demo.png)",
        }


class FakeMissingOpenVikingExplorerService(FakeOpenVikingExplorerService):
    async def openviking_read(self, **kwargs):
        return {
            "uri": kwargs["uri"],
            "level": kwargs["level"],
            "kind": "missing",
            "resolved_uri": kwargs["uri"],
            "note": f"File not found: {kwargs['uri']}",
            "candidate_uris": [],
            "content_markdown": "",
        }


class FakeDirectoryOpenVikingExplorerService(FakeOpenVikingExplorerService):
    async def openviking_read(self, **kwargs):
        return {
            "uri": kwargs["uri"],
            "level": kwargs["level"],
            "kind": "directory",
            "resolved_uri": "",
            "note": "Directory cannot be read directly. Pick one concrete text URI from candidate_uris.",
            "candidate_uris": [
                "viking://resources/demo/chapter-1.md",
                "viking://resources/demo/chapter-2.md",
            ],
            "content_markdown": "",
        }


def test_mcp_servers_keep_distinct_default_agent_ids() -> None:
    knowledge_server = KnowledgeMCPServer(
        config=Config(),
        explorer=FakeOpenVikingExplorerService(),
    )
    bid_server = BidMaterialMCPServer(
        config=Config(),
        service=FakeBidMaterialService(),
        explorer=FakeOpenVikingExplorerService(),
    )

    assert knowledge_server._agent_id == "knowledge-mcp"
    assert bid_server._agent_id == "bid-material-mcp"


def test_mcp_servers_respect_configured_agent_id() -> None:
    config = Config()
    config.ov_server.agent_id = "configured-agent"

    knowledge_server = KnowledgeMCPServer(
        config=config,
        explorer=FakeOpenVikingExplorerService(),
    )
    bid_server = BidMaterialMCPServer(
        config=config,
        service=FakeBidMaterialService(),
        explorer=FakeOpenVikingExplorerService(),
    )

    assert knowledge_server._agent_id == "configured-agent"
    assert bid_server._agent_id == "configured-agent"


@pytest.mark.asyncio
async def test_knowledge_mcp_initialize_and_tools_list_is_domain_neutral() -> None:
    server = KnowledgeMCPServer(
        config=Config(),
        explorer=FakeOpenVikingExplorerService(),
    )

    initialize = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    tools = await server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    resources = await server.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}}
    )

    assert initialize["result"]["capabilities"]["tools"]["listChanged"] is False
    assert initialize["result"]["capabilities"]["resources"]["listChanged"] is False
    assert initialize["result"]["serverInfo"]["name"] == "vikingbot-knowledge-base"
    assert resources["result"]["resources"][0]["name"] == "knowledge-root"
    assert "bid" not in resources["result"]["resources"][0]["description"].lower()
    assert {tool["name"] for tool in tools["result"]["tools"]} == {
        "openviking_search",
        "openviking_list",
        "openviking_glob",
        "openviking_read",
        "collect_evidence",
    }
    assert all("bid" not in tool["description"].lower() for tool in tools["result"]["tools"])
    read_tool = next(tool for tool in tools["result"]["tools"] if tool["name"] == "openviking_read")
    assert read_tool["inputSchema"]["properties"]["level"]["default"] == "read"


@pytest.mark.asyncio
async def test_knowledge_mcp_ignores_application_profile_tools() -> None:
    config = Config()
    config.agents.capability_profile = CapabilityProfile.BID_MATERIAL
    server = KnowledgeMCPServer(
        config=config,
        explorer=FakeOpenVikingExplorerService(),
    )

    tools = await server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )

    assert {tool["name"] for tool in tools["result"]["tools"]} == {
        "openviking_search",
        "openviking_list",
        "openviking_glob",
        "openviking_read",
        "collect_evidence",
    }


@pytest.mark.asyncio
async def test_knowledge_mcp_prompts_list_is_empty_for_client_probe() -> None:
    server = KnowledgeMCPServer(
        config=Config(),
        explorer=FakeOpenVikingExplorerService(),
    )

    response = await server.handle_message(
        {"jsonrpc": "2.0", "id": 4, "method": "prompts/list", "params": {}}
    )

    assert response["result"]["prompts"] == []


@pytest.mark.asyncio
async def test_bid_material_mcp_alias_keeps_profile_tools() -> None:
    config = Config()
    server = BidMaterialMCPServer(
        config=config,
        service=FakeBidMaterialService(),
        explorer=FakeOpenVikingExplorerService(),
        include_profile_tools=True,
    )

    initialize = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    )
    tools = await server.handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    resources = await server.handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}}
    )

    assert config.agents.capability_profile == CapabilityProfile.KNOWLEDGE_BASE
    assert initialize["result"]["serverInfo"]["name"] == "vikingbot-bid-material"
    assert resources["result"]["resources"][0]["name"] == "bid-material-root"
    assert {
        "search_certificates",
        "search_solution_materials",
        "collect_bid_evidence",
    }.issubset({tool["name"] for tool in tools["result"]["tools"]})
    solution_tool = next(
        tool for tool in tools["result"]["tools"] if tool["name"] == "search_solution_materials"
    )
    assert "max_images_per_item" not in solution_tool["inputSchema"]["properties"]


@pytest.mark.asyncio
async def test_mcp_tool_call_returns_text_payload() -> None:
    server = BidMaterialMCPServer(
        config=Config(),
        service=FakeBidMaterialService(),
        explorer=FakeOpenVikingExplorerService(),
    )

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
    server = KnowledgeMCPServer(
        config=Config(),
        explorer=FakeOpenVikingExplorerService(),
    )

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
    assert "![架构图](/tmp/demo.png)" in contents[0]["text"]


@pytest.mark.asyncio
async def test_mcp_collect_evidence_returns_generic_pack() -> None:
    server = KnowledgeMCPServer(
        config=Config(),
        explorer=FakeOpenVikingExplorerService(),
    )

    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {
                "name": "collect_evidence",
                "arguments": {"query": "部署方式", "target_uri": "viking://resources/"},
            },
        }
    )

    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["summary"] == "Collected 1 concrete evidence item(s)."
    assert payload["items"][0]["uri"] == "viking://resources/demo/doc.md"


@pytest.mark.asyncio
async def test_mcp_collect_evidence_excludes_missing_or_empty_reads() -> None:
    server = KnowledgeMCPServer(
        config=Config(),
        explorer=FakeMissingOpenVikingExplorerService(),
    )

    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "collect_evidence",
                "arguments": {"query": "部署方式", "target_uri": "viking://resources/"},
            },
        }
    )

    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["summary"] == "No concrete evidence was collected."
    assert payload["items"] == []
    assert payload["gaps"][0]["kind"] == "missing"
    assert payload["gaps"][0]["uri"] == "viking://resources/demo/doc.md"


@pytest.mark.asyncio
async def test_mcp_resources_read_returns_navigation_text_for_directory() -> None:
    server = KnowledgeMCPServer(
        config=Config(),
        explorer=FakeDirectoryOpenVikingExplorerService(),
    )

    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 8,
            "method": "resources/read",
            "params": {"uri": "viking://resources/demo"},
        }
    )

    text = response["result"]["contents"][0]["text"]
    assert "Directory cannot be read directly" in text
    assert "Candidate URIs:" in text
    assert "- viking://resources/demo/chapter-1.md" in text


@pytest.mark.asyncio
async def test_mcp_openviking_read_returns_structured_payload() -> None:
    server = BidMaterialMCPServer(
        config=Config(),
        service=FakeBidMaterialService(),
        explorer=FakeOpenVikingExplorerService(),
    )

    response = await server.handle_message(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "openviking_read",
                "arguments": {"uri": "viking://resources/demo/doc.md", "level": "read"},
            },
        }
    )

    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["kind"] == "document"
    assert payload["image_count"] == 1
    assert "![架构图](/tmp/demo.png)" in payload["content_markdown"]


def test_stdio_message_helpers_support_line_and_framed_protocols() -> None:
    line_message, line_mode = KnowledgeMCPServer._read_stdio_message(
        io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{}}\n')
    )
    assert line_mode == "line"
    assert line_message["method"] == "ping"

    framed_body = b'{"jsonrpc":"2.0","id":2,"method":"ping","params":{}}'
    framed_input = io.BytesIO(
        f"Content-Length: {len(framed_body)}\r\n\r\n".encode("ascii") + framed_body
    )
    framed_message, framed_mode = KnowledgeMCPServer._read_stdio_message(framed_input)
    assert framed_mode == "framed"
    assert framed_message["id"] == 2

    line_output = io.BytesIO()
    KnowledgeMCPServer._write_stdio_message(
        line_output,
        {"jsonrpc": "2.0", "id": 3, "result": {}},
        "line",
    )
    assert json.loads(line_output.getvalue().decode("utf-8"))["id"] == 3

    framed_output = io.BytesIO()
    KnowledgeMCPServer._write_stdio_message(
        framed_output,
        {"jsonrpc": "2.0", "id": 4, "result": {}},
        "framed",
    )
    assert framed_output.getvalue().startswith(b"Content-Length: ")
