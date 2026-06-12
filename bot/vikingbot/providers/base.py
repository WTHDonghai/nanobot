"""Base LLM provider interface."""

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

REQUIRED_TOOL_DISPATCH_NAME = "dispatch_required_tool"
ResponseDeltaCallback = Callable[[str], Awaitable[None]]


def build_required_tool_dispatch(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """Build one named function that preserves required tool selection semantics."""
    catalog: list[dict[str, Any]] = []
    names: list[str] = []
    for tool in tools:
        function = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        names.append(name)
        catalog.append(
            {
                "name": name,
                "description": function.get("description") or "",
                "parameters": function.get("parameters") or {"type": "object"},
            }
        )
    if not names:
        raise ValueError("Required tool dispatch needs at least one function tool")

    return {
        "type": "function",
        "function": {
            "name": REQUIRED_TOOL_DISPATCH_NAME,
            "description": (
                "Select and invoke exactly one tool from the catalog. Return its exact tool name "
                "and arguments. Tool catalog: "
                + json.dumps(catalog, ensure_ascii=False, separators=(",", ":"))
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {"type": "string", "enum": names},
                    "arguments": {"type": "object"},
                },
                "required": ["tool_name", "arguments"],
                "additionalProperties": False,
            },
        },
    }


def translate_required_tool_dispatch(
    response: "LLMResponse",
    tools: list[dict[str, Any]],
) -> "LLMResponse":
    """Translate the forced dispatcher call back into the selected real tool call."""
    allowed_names = {
        str(tool.get("function", {}).get("name") or "")
        for tool in tools
        if isinstance(tool, dict)
    }
    dispatch_calls = [
        call for call in response.tool_calls if call.name == REQUIRED_TOOL_DISPATCH_NAME
    ]
    if len(dispatch_calls) != 1:
        raise ValueError("Provider did not return the required tool dispatcher call")
    dispatch_call = dispatch_calls[0]
    selected_name = str(dispatch_call.arguments.get("tool_name") or "").strip()
    selected_arguments = dispatch_call.arguments.get("arguments")
    if selected_name not in allowed_names or not isinstance(selected_arguments, dict):
        raise ValueError("Provider returned an invalid required tool dispatch")
    response.tool_calls = [
        ToolCallRequest(
            id=dispatch_call.id,
            name=selected_name,
            arguments=selected_arguments,
            tokens=dispatch_call.tokens,
        )
    ]
    response.content = None
    return response


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]
    tokens: int


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None  # Kimi, DeepSeek-R1 etc.
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def has_tool_calls(self) -> bool:
        """Check if response contains tool calls."""
        return len(self.tool_calls) > 0


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.

    Implementations should handle the specifics of each provider's API
    while maintaining a consistent interface.
    """

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        session_id: str | None = None,
        on_delta: ResponseDeltaCallback | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions.
            tool_choice: Optional forced tool choice configuration.
            model: Model identifier (provider-specific).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            session_id: Optional session ID for tracing.
            on_delta: Optional callback for streaming final response text chunks.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        pass

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
        pass
