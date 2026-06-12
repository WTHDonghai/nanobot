"""LiteLLM provider implementation for multi-provider support."""

import json
import os
import re
from typing import Any

import litellm
from litellm import acompletion
from loguru import logger

from vikingbot.integrations.langfuse import LangfuseClient
from vikingbot.providers.base import (
    REQUIRED_TOOL_DISPATCH_NAME,
    LLMProvider,
    LLMResponse,
    ResponseDeltaCallback,
    ToolCallRequest,
    build_required_tool_dispatch,
    translate_required_tool_dispatch,
)
from vikingbot.providers.registry import find_by_model, find_gateway
from vikingbot.utils.helpers import cal_str_tokens


def _is_tool_choice_parameter_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "tool_choice" in message
        and (
            "invalid" in message
            or "badrequest" in message
            or "bad request" in message
            or "invalidparameter" in message
            or re.search(r"\b400\b", message) is not None
        )
    )


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.

    Supports OpenRouter, Anthropic, OpenAI, Gemini, MiniMax, and many other providers through
    a unified interface.  Provider-specific logic is driven by the registry
    (see providers/registry.py) — no if-elif chains needed here.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
        langfuse_client: LangfuseClient | None = None,
    ):
        super().__init__(api_key, api_base)
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        self.langfuse = langfuse_client or LangfuseClient.get_instance()

        # Detect gateway / local deployment.
        # provider_name (from config key) is the primary signal;
        # api_key / api_base are fallback for auto-detection.
        self._gateway = find_gateway(provider_name, api_key, api_base)

        # Configure environment variables
        if api_key:
            self._setup_env(api_key, api_base, default_model)

        if api_base:
            litellm.api_base = api_base

        # Disable LiteLLM logging noise
        litellm.suppress_debug_info = True
        # Drop unsupported parameters for providers (e.g., gpt-5 rejects some params)
        litellm.drop_params = True
        self._required_tool_choice_unsupported_models: set[str] = set()

    def _setup_env(self, api_key: str, api_base: str | None, model: str) -> None:
        """Set environment variables based on detected provider."""
        spec = self._gateway or find_by_model(model)
        if not spec:
            return

        # Gateway/local overrides existing env; standard provider doesn't
        if self._gateway:
            os.environ[spec.env_key] = api_key
        else:
            os.environ.setdefault(spec.env_key, api_key)

        # Resolve env_extras placeholders:
        #   {api_key}  → user's API key
        #   {api_base} → user's api_base, falling back to spec.default_api_base
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key)
            resolved = resolved.replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)

    def _resolve_model(self, model: str) -> str:
        """Resolve model name by applying provider/gateway prefixes."""
        if self._gateway:
            # Gateway mode: apply gateway prefix, skip provider-specific prefixes
            prefix = self._gateway.litellm_prefix
            if self._gateway.strip_model_prefix:
                model = model.split("/")[-1]
            if prefix and not model.startswith(f"{prefix}/"):
                model = f"{prefix}/{model}"
            return model

        # Standard mode: auto-prefix for known providers
        spec = find_by_model(model)
        if spec and spec.litellm_prefix:
            if not any(model.startswith(s) for s in spec.skip_prefixes):
                model = f"{spec.litellm_prefix}/{model}"

        return model

    def _apply_model_overrides(self, model: str, kwargs: dict[str, Any]) -> None:
        """Apply model-specific parameter overrides from the registry."""
        model_lower = model.lower()
        spec = find_by_model(model)
        if spec:
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    return

    @staticmethod
    def _disable_dashscope_thinking(
        model: str,
        kwargs: dict[str, Any],
    ) -> None:
        """Disable DashScope thinking mode for latency-sensitive bot calls."""
        spec = find_by_model(model)
        if not spec or spec.name != "dashscope":
            return
        extra_body = dict(kwargs.get("extra_body") or {})
        extra_body["enable_thinking"] = False
        kwargs["extra_body"] = extra_body

    def _handle_system_message(
        self, model: str, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """
        Handle system message for providers that don't support it (e.g. MiniMax).
        Merges system message into the first user message or converts to user role.
        """
        # Check for MiniMax
        if model.startswith("minimax/") or "/minimax/" in model:
            # Create a copy to avoid modifying the original list
            new_messages = []

            # Helper to merge content
            def merge_content(base_content, new_content):
                if isinstance(base_content, str) and isinstance(new_content, str):
                    return f"{new_content}\n\n{base_content}"
                if isinstance(base_content, list):
                    base_content = list(base_content)
                    base_content.insert(0, {"type": "text", "text": f"{new_content}\n\n"})
                    return base_content
                return f"{new_content}\n\n{str(base_content)}"

            # First pass: identify system messages
            system_contents = []
            cleaned_messages = []

            for msg in messages:
                if msg.get("role") == "system":
                    system_contents.append(msg.get("content", ""))
                else:
                    cleaned_messages.append(msg)

            # If no system messages, return as is
            if not system_contents:
                return messages

            # Combine all system prompts
            full_system_prompt = "\n\n".join([str(c) for c in system_contents])

            # Merge into the first user message if available
            merged = False
            for msg in cleaned_messages:
                if not merged and msg.get("role") == "user":
                    msg = msg.copy()
                    msg["content"] = merge_content(msg.get("content", ""), full_system_prompt)
                    new_messages.append(msg)
                    merged = True
                else:
                    new_messages.append(msg)

            # If no user message found, create one at the beginning
            if not merged:
                new_messages.insert(0, {"role": "user", "content": full_system_prompt})

            return new_messages

        return messages

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
        Send a chat completion request via LiteLLM.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            tool_choice: Optional forced tool choice configuration.
            model: Model identifier (e.g., 'anthropic/claude-sonnet-4-5').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            session_id: Optional session ID for tracing.
            on_delta: Optional callback for streaming final response text chunks.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        model = self._resolve_model(model or self.default_model)

        # Handle system message for MiniMax and others that don't support it
        messages = self._handle_system_message(model, messages)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        # Apply model-specific overrides (e.g. kimi-k2.5 temperature)
        self._apply_model_overrides(model, kwargs)
        self._disable_dashscope_thinking(model, kwargs)

        # Pass api_key directly — more reliable than env vars alone
        if self.api_key:
            kwargs["api_key"] = self.api_key

        # Pass api_base for custom endpoints
        if self.api_base:
            kwargs["api_base"] = self.api_base

        # Pass extra headers (e.g. APP-Code for AiHubMix)
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers

        required_dispatch = False
        if tools:
            effective_tool_choice = tool_choice if tool_choice is not None else "auto"
            if effective_tool_choice == "required":
                self._disable_dashscope_thinking(model, kwargs)
            if (
                effective_tool_choice == "required"
                and model in self._required_tool_choice_unsupported_models
            ):
                required_dispatch = True
                kwargs["tools"] = [build_required_tool_dispatch(tools)]
                kwargs["tool_choice"] = {
                    "type": "function",
                    "function": {"name": REQUIRED_TOOL_DISPATCH_NAME},
                }
            else:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = effective_tool_choice

        # Langfuse integration
        # Note: session_id is set via propagate_attributes in loop.py, not here
        langfuse_observation = None
        try:
            if self.langfuse.enabled and self.langfuse._client:
                metadata = {"has_tools": tools is not None}
                client = self.langfuse._client
                # Use start_observation with generation type
                if hasattr(client, "start_observation"):
                    langfuse_observation = client.start_observation(
                        name="llm-chat",
                        as_type="generation",
                        model=model,
                        input=messages,
                        metadata=metadata,
                    )

            try:
                if on_delta and not tools:
                    try:
                        llm_response = await self._stream_chat_response(kwargs, on_delta)
                        response = None
                    except Exception as stream_error:
                        logger.warning(
                            "[LLM_STREAM] LiteLLM streaming failed; retrying non-stream "
                            f"model={model} session_id={session_id}: {stream_error}"
                        )
                        response = await acompletion(**kwargs)
                        llm_response = self._parse_response(response)
                else:
                    response = await acompletion(**kwargs)
                    llm_response = self._parse_response(response)
            except Exception as e:
                if kwargs.get("tool_choice") == "required" and _is_tool_choice_parameter_error(e):
                    logger.warning(
                        "[LLM_COMPAT] Retrying LiteLLM chat with a named required-tool dispatcher "
                        f"after required was rejected model={model} session_id={session_id}: {e}"
                    )
                    self._required_tool_choice_unsupported_models.add(model)
                    required_dispatch = True
                    kwargs["tools"] = [build_required_tool_dispatch(tools or [])]
                    kwargs["tool_choice"] = {
                        "type": "function",
                        "function": {"name": REQUIRED_TOOL_DISPATCH_NAME},
                    }
                    self._disable_dashscope_thinking(model, kwargs)
                    response = await acompletion(**kwargs)
                    llm_response = self._parse_response(response)
                else:
                    raise
            if required_dispatch:
                llm_response = translate_required_tool_dispatch(llm_response, tools or [])
                llm_response.metadata["effective_tool_choice"] = "required_dispatch"
            else:
                llm_response.metadata["effective_tool_choice"] = kwargs.get("tool_choice")

            # Update and end Langfuse observation
            if langfuse_observation:
                output_text = llm_response.content or ""
                if llm_response.tool_calls:
                    output_text = (
                        output_text
                        or f"[Tool calls: {[tc.name for tc in llm_response.tool_calls]}]"
                    )

                # Update observation with output and usage
                update_kwargs: dict[str, Any] = {
                    "output": output_text,
                    "metadata": {"finish_reason": llm_response.finish_reason},
                }

                if llm_response.usage:
                    # Add usage data using usage_details format
                    usage_details: dict[str, Any] = {
                        "input": llm_response.usage.get("prompt_tokens", 0),
                        "output": llm_response.usage.get("completion_tokens", 0),
                    }

                    # Add cache read tokens if available
                    cache_read_tokens = llm_response.usage.get(
                        "cache_read_input_tokens"
                    ) or llm_response.usage.get("prompt_tokens_details", {}).get("cached_tokens")
                    if cache_read_tokens:
                        usage_details["cache_read_input_tokens"] = cache_read_tokens

                    update_kwargs["usage_details"] = usage_details

                # Update the observation
                if hasattr(langfuse_observation, "update"):
                    try:
                        langfuse_observation.update(**update_kwargs)
                    except Exception as e:
                        logger.debug(f"[LANGFUSE] Failed to update observation: {e}")

                # End the observation
                if hasattr(langfuse_observation, "end"):
                    try:
                        langfuse_observation.end()
                    except Exception as e:
                        logger.debug(f"[LANGFUSE] Failed to end observation: {e}")

                try:
                    self.langfuse.flush()
                except Exception as e:
                    logger.debug(f"[LANGFUSE] Failed to flush: {e}")

            return llm_response
        except Exception as e:
            logger.exception(
                "[LLM_ERROR] LiteLLM chat failed "
                f"model={model} tool_choice={kwargs.get('tool_choice')} "
                f"tools={len(tools or [])} session_id={session_id}: {e}"
            )
            # End Langfuse observation with error
            if langfuse_observation:
                try:
                    if hasattr(langfuse_observation, "update"):
                        langfuse_observation.update(
                            output=f"Error: {str(e)}",
                            metadata={"error": str(e)},
                        )
                    if hasattr(langfuse_observation, "end"):
                        langfuse_observation.end()
                    try:
                        self.langfuse.flush()
                    except Exception:
                        pass
                except Exception:
                    pass
            # Return error as content for graceful handling
            return LLMResponse(
                content=f"Error calling LLM: {str(e)}",
                finish_reason="error",
            )

    @staticmethod
    def _field(obj: Any, key: str, default: Any = None) -> Any:
        """Read a value from LiteLLM objects and dict-like streaming chunks."""
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    @classmethod
    def _parse_usage_obj(cls, usage_obj: Any) -> dict[str, int]:
        """Parse token usage from either object or dict responses."""
        if not usage_obj:
            return {}

        prompt_tokens = cls._field(usage_obj, "prompt_tokens", 0) or 0
        completion_tokens = cls._field(usage_obj, "completion_tokens", 0) or 0
        total_tokens = cls._field(usage_obj, "total_tokens", 0) or 0
        usage = {
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "total_tokens": int(total_tokens),
        }

        details = cls._field(usage_obj, "prompt_tokens_details")
        cached = cls._field(details, "cached_tokens") if details else None
        cached = cached or cls._field(usage_obj, "cache_read_input_tokens")
        if cached:
            usage["cache_read_input_tokens"] = int(cached)
        return usage

    @classmethod
    def _first_choice(cls, response_or_chunk: Any) -> Any | None:
        choices = cls._field(response_or_chunk, "choices") or []
        return choices[0] if choices else None

    async def _stream_chat_response(
        self,
        kwargs: dict[str, Any],
        on_delta: ResponseDeltaCallback,
    ) -> LLMResponse:
        """Stream a plain-text chat completion and accumulate the final response."""
        stream = await acompletion(**kwargs, stream=True)
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        finish_reason = "stop"
        usage: dict[str, int] = {}

        async def handle_chunk(chunk: Any) -> None:
            nonlocal finish_reason, usage
            if parsed_usage := self._parse_usage_obj(self._field(chunk, "usage")):
                usage = parsed_usage

            choice = self._first_choice(chunk)
            if choice is None:
                return
            if reason := self._field(choice, "finish_reason"):
                finish_reason = str(reason)

            delta = self._field(choice, "delta")
            if not delta:
                return

            reasoning = self._field(delta, "reasoning_content") or self._field(
                delta, "reasoning"
            )
            if reasoning:
                reasoning_parts.append(str(reasoning))

            content = self._field(delta, "content")
            if not content:
                return
            text = str(content)
            content_parts.append(text)
            await on_delta(text)

        if hasattr(stream, "__aiter__"):
            async for chunk in stream:
                await handle_chunk(chunk)
        else:
            for chunk in stream:
                await handle_chunk(chunk)

        return LLMResponse(
            content="".join(content_parts),
            finish_reason=finish_reason,
            usage=usage,
            reasoning_content="".join(reasoning_parts) or None,
        )

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
        choice = response.choices[0]
        message = choice.message

        tool_calls = []
        if hasattr(message, "tool_calls") and message.tool_calls:
            for tc in message.tool_calls:
                # Parse arguments from JSON string if needed
                args = tc.function.arguments
                tokens = cal_str_tokens(tc.function.name, text_type="en")
                if isinstance(args, str):
                    try:
                        tokens += cal_str_tokens(args, text_type="mixed")
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}

                tool_calls.append(
                    ToolCallRequest(id=tc.id, name=tc.function.name, arguments=args, tokens=tokens)
                )

        usage = self._parse_usage_obj(getattr(response, "usage", None))

        reasoning_content = getattr(message, "reasoning_content", None)

        return LLMResponse(
            content=message.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
            reasoning_content=reasoning_content,
        )

    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
