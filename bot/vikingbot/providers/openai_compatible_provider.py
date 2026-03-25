"""HTTP-based OpenAI-compatible provider without LiteLLM dependency."""

from __future__ import annotations

import json
from typing import Any

import httpx

from vikingbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from vikingbot.providers.registry import find_by_model, find_by_name, find_gateway
from vikingbot.utils.helpers import cal_str_tokens


class OpenAICompatibleProvider(LLMProvider):
    """Minimal OpenAI-compatible chat provider backed by httpx."""

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "gpt-4o-mini",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
        timeout: float = 120.0,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self.provider_name = provider_name or ""
        self.timeout = timeout
        self._gateway = find_gateway(self.provider_name, api_key, api_base)
        self._spec = self._gateway or find_by_name(self.provider_name) or find_by_model(default_model)

    def _resolve_api_base(self) -> str:
        if self.api_base:
            return self.api_base.rstrip("/")
        if self._spec and self._spec.default_api_base:
            return self._spec.default_api_base.rstrip("/")
        return "https://api.openai.com/v1"

    def _resolve_endpoint(self) -> str:
        base = self._resolve_api_base()
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _resolve_model(self, model: str | None) -> str:
        resolved = model or self.default_model
        if "/" not in resolved:
            return resolved

        prefix, remainder = resolved.split("/", 1)
        known_prefixes = {
            "openai",
            "openrouter",
            "deepseek",
            "dashscope",
            "moonshot",
            "minimax",
            "groq",
            "volcengine",
            "hosted_vllm",
            "zai",
            "zhipu",
            "gemini",
        }
        if prefix in known_prefixes:
            return remainder
        return resolved

    @staticmethod
    def _merge_content(base_content: Any, new_content: str) -> Any:
        if isinstance(base_content, str):
            return f"{new_content}\n\n{base_content}"
        if isinstance(base_content, list):
            items = list(base_content)
            items.insert(0, {"type": "text", "text": f"{new_content}\n\n"})
            return items
        return f"{new_content}\n\n{base_content}"

    def _handle_system_message(self, model: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not ("minimax" in model.lower() or self.provider_name == "minimax"):
            return messages

        system_contents: list[str] = []
        cleaned_messages: list[dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system_contents.append(str(msg.get("content", "")))
            else:
                cleaned_messages.append(dict(msg))

        if not system_contents:
            return messages

        system_prompt = "\n\n".join(system_contents)
        merged = False
        new_messages: list[dict[str, Any]] = []
        for msg in cleaned_messages:
            if not merged and msg.get("role") == "user":
                msg["content"] = self._merge_content(msg.get("content", ""), system_prompt)
                merged = True
            new_messages.append(msg)

        if not merged:
            new_messages.insert(0, {"role": "user", "content": system_prompt})
        return new_messages

    @staticmethod
    def _extract_text_content(content: Any) -> str | None:
        if isinstance(content, str) or content is None:
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text" and item.get("text"):
                        text_parts.append(str(item["text"]))
                    elif item.get("type") == "output_text" and item.get("text"):
                        text_parts.append(str(item["text"]))
            return "".join(text_parts) if text_parts else None
        return str(content)

    def _build_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)
        return headers

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        tool_choice: Any | None,
        model: str | None,
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        resolved_model = self._resolve_model(model)
        normalized_messages = self._handle_system_message(resolved_model, messages)
        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": normalized_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice if tool_choice is not None else "auto"
        return payload

    @staticmethod
    def _parse_usage(data: dict[str, Any]) -> dict[str, int]:
        usage = data.get("usage") or {}
        parsed = {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        }
        prompt_details = usage.get("prompt_tokens_details") or {}
        cached_tokens = prompt_details.get("cached_tokens") or usage.get("cache_read_input_tokens")
        if cached_tokens:
            parsed["cache_read_input_tokens"] = int(cached_tokens)
        return parsed

    @staticmethod
    def _parse_tool_calls(message: dict[str, Any]) -> list[ToolCallRequest]:
        tool_calls: list[ToolCallRequest] = []
        for tc in message.get("tool_calls") or []:
            function = tc.get("function") or {}
            args = function.get("arguments", {})
            tokens = cal_str_tokens(function.get("name", ""), text_type="en")
            if isinstance(args, str):
                try:
                    tokens += cal_str_tokens(args, text_type="mixed")
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            tool_calls.append(
                ToolCallRequest(
                    id=str(tc.get("id") or ""),
                    name=str(function.get("name") or ""),
                    arguments=args if isinstance(args, dict) else {"raw": args},
                    tokens=tokens,
                )
            )
        return tool_calls

    def _parse_response(self, data: dict[str, Any]) -> LLMResponse:
        choices = data.get("choices") or []
        if not choices:
            return LLMResponse(content="Error calling LLM: empty response", finish_reason="error")

        choice = choices[0]
        message = choice.get("message") or {}
        return LLMResponse(
            content=self._extract_text_content(message.get("content")),
            tool_calls=self._parse_tool_calls(message),
            finish_reason=str(choice.get("finish_reason") or "stop"),
            usage=self._parse_usage(data),
            reasoning_content=message.get("reasoning_content") or message.get("reasoning"),
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        session_id: str | None = None,
    ) -> LLMResponse:
        del session_id
        payload = self._build_payload(messages, tools, tool_choice, model, max_tokens, temperature)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self._resolve_endpoint(),
                    headers=self._build_headers(),
                    json=payload,
                )
                response.raise_for_status()
                return self._parse_response(response.json())
        except Exception as e:
            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
            )

    def get_default_model(self) -> str:
        return self.default_model
