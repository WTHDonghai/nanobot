# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for OpenAI-compatible provider request construction."""

from types import SimpleNamespace

import pytest
from vikingbot.providers.base import REQUIRED_TOOL_DISPATCH_NAME
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
    assert response.metadata["effective_tool_choice"] == "required"


@pytest.mark.asyncio
async def test_openai_compatible_provider_retries_required_with_named_dispatch() -> None:
    provider = OpenAICompatibleProvider(
        api_key="test-key",
        api_base="https://example.com/v1",
        default_model="stub-model",
    )

    captured_tool_choices: list[str] = []

    async def create(**kwargs):
        captured_tool_choices.append(kwargs.get("tool_choice"))
        if len(captured_tool_choices) == 1:
            raise Exception(
                "BadRequestError: <400> InternalError.Algo.InvalidParameter: The tool_choice parameter is invalid"
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id=f"dispatch-{len(captured_tool_choices)}",
                                function=SimpleNamespace(
                                    name=REQUIRED_TOOL_DISPATCH_NAME,
                                    arguments=(
                                        '{"tool_name":"openviking_search",'
                                        '"arguments":{"query":"status C"}}'
                                    ),
                                ),
                            )
                        ],
                    ),
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

    assert response.content is None
    assert response.tool_calls[0].name == "openviking_search"
    assert response.tool_calls[0].arguments == {"query": "status C"}
    assert captured_tool_choices == [
        "required",
        {"type": "function", "function": {"name": REQUIRED_TOOL_DISPATCH_NAME}},
    ]

    second_response = await provider.chat(
        messages=[{"role": "user", "content": "hello again"}],
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

    assert second_response.tool_calls[0].name == "openviking_search"
    assert captured_tool_choices[-1] == {
        "type": "function",
        "function": {"name": REQUIRED_TOOL_DISPATCH_NAME},
    }
    assert response.metadata["effective_tool_choice"] == "required_dispatch"
    assert second_response.metadata["effective_tool_choice"] == "required_dispatch"
