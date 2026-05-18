"""Compatibility wrapper for the bid-material MCP entrypoint."""

from __future__ import annotations

from typing import Any

from vikingbot.config.schema import Config
from vikingbot.mcp.knowledge_server import MCPToolDefinition, KnowledgeMCPServer
from vikingbot.services.bid_material import BidMaterialService
from vikingbot.services.openviking_explorer import DEFAULT_TARGET_URI, OpenVikingExplorerService


class BidMaterialMCPServer(KnowledgeMCPServer):
    """Backward-compatible MCP server that includes bid-material tools."""

    server_name = "vikingbot-bid-material"
    resource_name = "bid-material-root"
    resource_description = "OpenViking bid-material resource root."
    default_agent_id = "bid-material-mcp"

    def __init__(
        self,
        config: Config | None = None,
        service: BidMaterialService | None = None,
        explorer: OpenVikingExplorerService | None = None,
        **kwargs: Any,
    ) -> None:
        kwargs.pop("include_profile_tools", None)
        self.service = service
        super().__init__(config=config, explorer=explorer, **kwargs)

    def _bid_material_service(self) -> BidMaterialService:
        if self.service is None:
            self.service = BidMaterialService(agent_id=self._agent_id)
        return self.service

    def _build_tools(self) -> dict[str, MCPToolDefinition]:
        tools = super()._build_tools()
        tools.update(
            {
                "search_certificates": MCPToolDefinition(
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
                "search_solution_materials": MCPToolDefinition(
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
                "collect_bid_evidence": MCPToolDefinition(
                    name="collect_bid_evidence",
                    description="Collect reusable evidence for one bid-response section. Do not draft the final section from memory before using this evidence. Each item contains markdown-ready evidence content.",
                    input_schema={
                        "type": "object",
                        "properties": {
                            "section_name": {"type": "string", "description": "Bid section name"},
                            "requirement": {
                                "type": "string",
                                "description": "Requirement or section brief",
                            },
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
            }
        )
        return tools

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "search_certificates":
            service = self._bid_material_service()
            pack = await service.search_certificates(
                query=str(arguments["query"]),
                target_uri=str(arguments.get("target_uri") or DEFAULT_TARGET_URI),
                top_k=int(arguments.get("top_k", 5)),
            )
            return service.pack_to_json(pack)

        if name == "search_solution_materials":
            service = self._bid_material_service()
            pack = await service.search_solution_materials(
                query=str(arguments["query"]),
                target_uri=str(arguments.get("target_uri") or DEFAULT_TARGET_URI),
                top_k=int(arguments.get("top_k", 5)),
            )
            return service.pack_to_json(pack)

        if name == "collect_bid_evidence":
            service = self._bid_material_service()
            pack = await service.collect_bid_evidence(
                section_name=str(arguments["section_name"]),
                requirement=str(arguments["requirement"]),
                target_uri=str(arguments.get("target_uri") or DEFAULT_TARGET_URI),
                top_k=int(arguments.get("top_k", 8)),
            )
            return service.pack_to_json(pack)

        return await super()._call_tool(name, arguments)
