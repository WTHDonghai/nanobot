"""Agent tool for requesting a human handoff."""

from __future__ import annotations

from typing import Any

from vikingbot.agent.tools.base import Tool, ToolContext
from vikingbot.services.human_handoff import HumanHandoffPayload, HumanHandoffService


class HumanHandoffTool(Tool):
    """Request a human support handoff for the current conversation."""

    def __init__(self, handoff_service: HumanHandoffService):
        self._handoff_service = handoff_service

    @property
    def name(self) -> str:
        return "human_handoff"

    @property
    def description(self) -> str:
        return (
            "Transfer the current conversation to a human support agent. "
            "Use this only when the user explicitly asks for human help or confirms a handoff."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Why the conversation should be transferred to a human agent.",
                },
                "summary": {
                    "type": "string",
                    "description": "Short summary of the issue for the human agent.",
                },
                "latest_user_message": {
                    "type": "string",
                    "description": "Latest user message that triggered the handoff.",
                },
                "latest_assistant_message": {
                    "type": "string",
                    "description": "Latest assistant message shown before the handoff.",
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional structured metadata to attach to the handoff request.",
                },
            },
        }

    async def execute(self, tool_context: ToolContext, **kwargs: Any) -> str:
        result = await self._handoff_service.request_handoff(
            HumanHandoffPayload(
                session_id=tool_context.session_key.chat_id if tool_context.session_key else None,
                user_id=tool_context.sender_id,
                reason=kwargs.get("reason"),
                summary=kwargs.get("summary"),
                latest_user_message=kwargs.get("latest_user_message"),
                latest_assistant_message=kwargs.get("latest_assistant_message"),
                source="agent_tool",
                metadata={
                    "channel_type": tool_context.session_key.type if tool_context.session_key else None,
                    "channel_id": (
                        tool_context.session_key.channel_id if tool_context.session_key else None
                    ),
                    **(kwargs.get("metadata") or {}),
                },
            )
        )

        parts = [result.message]
        if result.handoff_id:
            parts.append(f"handoff_id={result.handoff_id}")
        if result.entry_url:
            parts.append(f"entry_url={result.entry_url}")
        return "\n".join(parts)
