"""LLM provider abstraction module."""

from vikingbot.providers.base import LLMProvider, LLMResponse
from vikingbot.providers.openai_compatible_provider import OpenAICompatibleProvider

__all__ = ["LLMProvider", "LLMResponse", "OpenAICompatibleProvider"]
