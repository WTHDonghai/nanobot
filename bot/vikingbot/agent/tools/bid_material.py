"""Agent tools for bid-material retrieval."""

from __future__ import annotations

from typing import Any

from vikingbot.agent.tools.base import Tool, ToolContext
from vikingbot.services.bid_material import BidMaterialService, render_evidence_pack_for_agent


class _BidMaterialTool(Tool):
    def __init__(self) -> None:
        self._service = BidMaterialService()


class SearchCertificatesTool(_BidMaterialTool):
    @property
    def name(self) -> str:
        return "search_certificates"

    @property
    def description(self) -> str:
        return "Search qualification certificates, licenses, and authorization materials. Use this before drafting qualification sections. Returns reusable markdown evidence snippets with inline images."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
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
                    "description": "Maximum number of evidence items to return",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        tool_context: ToolContext,
        query: str,
        target_uri: str = "viking://resources/",
        top_k: int = 5,
        **kwargs: Any,
    ) -> str:
        pack = await self._service.search_certificates(
            query=query,
            target_uri=target_uri,
            top_k=top_k,
            context_id=tool_context.workspace_id,
        )
        return render_evidence_pack_for_agent(pack)


class SearchSolutionMaterialsTool(_BidMaterialTool):
    @property
    def name(self) -> str:
        return "search_solution_materials"

    @property
    def description(self) -> str:
        return "Search solution, product, case-study, and parameter materials. Read returned source URIs before drafting long-form prose. Returns markdown evidence snippets with inline images."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
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
                    "description": "Maximum number of evidence items to return",
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(
        self,
        tool_context: ToolContext,
        query: str,
        target_uri: str = "viking://resources/",
        top_k: int = 5,
        **kwargs: Any,
    ) -> str:
        pack = await self._service.search_solution_materials(
            query=query,
            target_uri=target_uri,
            top_k=top_k,
            context_id=tool_context.workspace_id,
        )
        return render_evidence_pack_for_agent(pack)


class CollectBidEvidenceTool(_BidMaterialTool):
    @property
    def name(self) -> str:
        return "collect_bid_evidence"

    @property
    def description(self) -> str:
        return "Collect reusable evidence for one bid-response section and identify missing claims. Do not draft the final section from memory before using this evidence."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
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
                    "description": "Maximum number of merged evidence items to return",
                    "default": 8,
                },
            },
            "required": ["section_name", "requirement"],
        }

    async def execute(
        self,
        tool_context: ToolContext,
        section_name: str,
        requirement: str,
        target_uri: str = "viking://resources/",
        top_k: int = 8,
        **kwargs: Any,
    ) -> str:
        pack = await self._service.collect_bid_evidence(
            section_name=section_name,
            requirement=requirement,
            target_uri=target_uri,
            top_k=top_k,
            context_id=tool_context.workspace_id,
        )
        return render_evidence_pack_for_agent(pack)
