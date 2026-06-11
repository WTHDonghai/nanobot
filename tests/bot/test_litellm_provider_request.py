# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for LiteLLM provider request construction."""

from types import SimpleNamespace

import pytest
from vikingbot.providers import litellm_provider
from vikingbot.providers.base import REQUIRED_TOOL_DISPATCH_NAME
from vikingbot.providers.litellm_provider import LiteLLMProvider


@pytest.mark.asyncio
async def test_litellm_provider_records_effective_tool_choice(monkeypatch) -> None:
    provider = LiteLLMProvider(api_key="test-key", default_model="openai/gpt-4o")
    captured: dict = {}

    async def fake_acompletion(**kwargs):
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

    monkeypatch.setattr(litellm_provider, "acompletion", fake_acompletion)

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
        model="openai/gpt-4o",
        temperature=0,
    )

    assert response.content == "ok"
    assert captured["tool_choice"] == "required"
    assert response.metadata["effective_tool_choice"] == "required"


@pytest.mark.asyncio
async def test_litellm_dashscope_required_tool_choice_disables_thinking(monkeypatch) -> None:
    provider = LiteLLMProvider(api_key="test-key", default_model="dashscope/qwen3.6-flash")
    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="search-1",
                                function=SimpleNamespace(
                                    name="openviking_search",
                                    arguments='{"query":"宾客状态"}',
                                ),
                            )
                        ],
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    monkeypatch.setattr(litellm_provider, "acompletion", fake_acompletion)

    response = await provider.chat(
        messages=[{"role": "user", "content": "向我介绍宾客状态"}],
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
        model="dashscope/qwen3.6-flash",
        temperature=0,
    )

    assert captured["tool_choice"] == "required"
    assert captured["extra_body"] == {"enable_thinking": False}
    assert response.tool_calls[0].name == "openviking_search"
    assert response.metadata["effective_tool_choice"] == "required"


@pytest.mark.asyncio
async def test_litellm_dashscope_dispatch_retry_also_disables_thinking(monkeypatch) -> None:
    provider = LiteLLMProvider(api_key="test-key", default_model="dashscope/qwen3.6-flash")
    captured: list[dict] = []

    async def fake_acompletion(**kwargs):
        captured.append(kwargs)
        if len(captured) == 1:
            raise Exception(
                "400 InternalError.Algo.InvalidParameter: The tool_choice parameter does not "
                "support being set to required or object in thinking mode"
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="dispatch-1",
                                function=SimpleNamespace(
                                    name=REQUIRED_TOOL_DISPATCH_NAME,
                                    arguments=(
                                        '{"tool_name":"openviking_search",'
                                        '"arguments":{"query":"宾客状态"}}'
                                    ),
                                ),
                            )
                        ],
                    ),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    monkeypatch.setattr(litellm_provider, "acompletion", fake_acompletion)

    response = await provider.chat(
        messages=[{"role": "user", "content": "向我介绍宾客状态"}],
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
        model="dashscope/qwen3.6-flash",
        temperature=0,
    )

    assert [request["extra_body"] for request in captured] == [
        {"enable_thinking": False},
        {"enable_thinking": False},
    ]
    assert captured[1]["tool_choice"] == {
        "type": "function",
        "function": {"name": REQUIRED_TOOL_DISPATCH_NAME},
    }
    assert response.tool_calls[0].name == "openviking_search"
    assert response.metadata["effective_tool_choice"] == "required_dispatch"


@pytest.mark.asyncio
async def test_litellm_provider_retries_required_with_named_dispatch(monkeypatch) -> None:
    provider = LiteLLMProvider(api_key="test-key", default_model="openai/gpt-4o")
    captured: list[dict] = []

    async def fake_acompletion(**kwargs):
        captured.append(kwargs)
        if len(captured) == 1:
            raise Exception("400 invalid tool_choice parameter")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=None,
                        tool_calls=[
                            SimpleNamespace(
                                id="dispatch-1",
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
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    monkeypatch.setattr(litellm_provider, "acompletion", fake_acompletion)
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
        model="openai/gpt-4o",
        temperature=0,
    )

    assert captured[1]["tool_choice"] == {
        "type": "function",
        "function": {"name": REQUIRED_TOOL_DISPATCH_NAME},
    }
    assert response.tool_calls[0].name == "openviking_search"
    assert response.metadata["effective_tool_choice"] == "required_dispatch"
