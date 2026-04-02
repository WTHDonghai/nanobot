# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for OpenAI-compatible provider request construction."""

from types import SimpleNamespace

import pytest

from vikingbot.providers.openai_compatible_provider import OpenAICompatibleProvider


@pytest.mark.asyncio
async def test_openai_compatible_provider_passes_through_tool_choice() -> None:
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        api_base="https://example.com/v1",
        default_model="stub-model",
    )

    captured: dict = {}

    async def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=[]),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
            ),
        )

    provider.client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )

    response = await provider.chat(
        messages=[{"role": "user", "content": "hello"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "openviking_search",
                    "description": "Search docs",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
        tool_choice="required",
        model="stub-model",
        temperature=0,
    )

    assert response.content == "ok"
    assert captured["tool_choice"] == "required"
