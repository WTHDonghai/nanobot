# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for DingTalk channel interactive-card rendering."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from vikingbot.bus.events import OutboundEventType, OutboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.channels.dingtalk import (
    DINGTALK_PLACEHOLDER_TEXT,
    DingTalkCardState,
    DingTalkChannel,
)
from vikingbot.config.schema import DingTalkChannelConfig, SessionKey


def _build_live_state(
    channel: DingTalkChannel, session_key: SessionKey, card_biz_id: str
) -> DingTalkCardState:
    state = DingTalkCardState(
        session_key=session_key.safe_name(),
        chat_id=session_key.chat_id,
        single_chat_receiver=channel._make_single_chat_receiver(session_key.chat_id),
        card_biz_id=card_biz_id,
        last_card_data="placeholder-card",
    )
    channel._remember_card_state(state)
    return state


@pytest.mark.asyncio
async def test_dingtalk_send_rewrites_send_images_to_media_ids(monkeypatch) -> None:
    channel = DingTalkChannel(
        DingTalkChannelConfig(client_id="robot-code", client_secret="secret"),
        MessageBus(),
    )
    channel._http = AsyncMock()
    channel._http.put = AsyncMock(return_value=httpx.Response(200, json={}))
    channel._get_access_token = AsyncMock(return_value="token")
    channel._parse_data_uri = AsyncMock(return_value=(False, b"image-bytes"))
    monkeypatch.setattr(
        channel,
        "_upload_image_to_dingtalk",
        AsyncMock(return_value="media-id-123"),
    )
    session_key = SessionKey(type="dingtalk", channel_id="robot-code", chat_id="user-1")
    _build_live_state(channel, session_key, "card-1")

    await channel.send(
        OutboundMessage(
            session_key=session_key,
            content="请参考下图：\n![流程图](send://workflow.png)",
            metadata={"card_biz_id": "card-1"},
        )
    )

    request_json = channel._http.put.await_args.kwargs["json"]
    card_data = json.loads(request_json["cardData"])
    markdown_blocks = [
        block["text"] for block in card_data["contents"] if block.get("type") == "markdown"
    ]

    assert request_json["cardBizId"] == "card-1"
    assert any("media-id-123" in block for block in markdown_blocks)
    assert all("send://workflow.png" not in block for block in markdown_blocks)


@pytest.mark.asyncio
async def test_dingtalk_final_card_hides_loading_status() -> None:
    channel = DingTalkChannel(
        DingTalkChannelConfig(client_id="robot-code", client_secret="secret"),
        MessageBus(),
    )
    channel._http = AsyncMock()
    channel._http.put = AsyncMock(return_value=httpx.Response(200, json={}))
    channel._get_access_token = AsyncMock(return_value="token")
    session_key = SessionKey(type="dingtalk", channel_id="robot-code", chat_id="user-1")
    _build_live_state(channel, session_key, "card-1")

    await channel.send(
        OutboundMessage(
            session_key=session_key,
            content="最终回复内容",
            metadata={"card_biz_id": "card-1"},
        )
    )

    request_json = channel._http.put.await_args.kwargs["json"]
    card_data = json.loads(request_json["cardData"])
    ids = [block.get("id") for block in card_data["contents"]]

    assert request_json["cardBizId"] == "card-1"
    assert "status_text" not in ids
    assert "status_divider" not in ids
    assert any(block["text"] == "最终回复内容" for block in card_data["contents"] if block.get("id") == "content_markdown")


@pytest.mark.asyncio
async def test_dingtalk_send_renders_error_card_when_inline_image_upload_fails(monkeypatch) -> None:
    channel = DingTalkChannel(
        DingTalkChannelConfig(client_id="robot-code", client_secret="secret"),
        MessageBus(),
    )
    channel._http = AsyncMock()
    channel._http.put = AsyncMock(return_value=httpx.Response(200, json={}))
    channel._http.post = AsyncMock(return_value=httpx.Response(200, json={}))
    channel._get_access_token = AsyncMock(return_value="token")
    channel._parse_data_uri = AsyncMock(return_value=(False, b"image-bytes"))
    monkeypatch.setattr(
        channel,
        "_upload_image_to_dingtalk",
        AsyncMock(side_effect=RuntimeError("upload failed")),
    )
    session_key = SessionKey(type="dingtalk", channel_id="robot-code", chat_id="user-1")
    _build_live_state(channel, session_key, "card-1")

    await channel.send(
        OutboundMessage(
            session_key=session_key,
            content="请参考下图：\n![流程图](send://workflow.png)",
            metadata={"card_biz_id": "card-1"},
        )
    )

    request_json = channel._http.put.await_args.kwargs["json"]
    card_data = json.loads(request_json["cardData"])
    markdown_blocks = [block["text"] for block in card_data["contents"] if block.get("type") == "markdown"]

    assert request_json["cardBizId"] == "card-1"
    assert any("消息生成失败，请稍后重试。" in block for block in markdown_blocks)


@pytest.mark.asyncio
async def test_dingtalk_send_splits_long_plain_text_into_card_blocks() -> None:
    channel = DingTalkChannel(
        DingTalkChannelConfig(client_id="robot-code", client_secret="secret"),
        MessageBus(),
    )
    channel._http = AsyncMock()
    channel._http.put = AsyncMock(return_value=httpx.Response(200, json={}))
    channel._get_access_token = AsyncMock(return_value="token")

    paragraph_one = "这是一段较长的纯文本回复，用来验证钉钉长消息的安全分段发送能力。" * 3
    paragraph_two = "这里继续补充第二段说明，确认纯文本不会继续走 markdown 模板导致展示裁切。" * 3
    paragraph_three = "最后一段用于确保超过单条建议长度后，渠道会拆成多条 sampleText 消息发送。" * 3
    content = "\n\n".join([paragraph_one, paragraph_two, paragraph_three])
    session_key = SessionKey(type="dingtalk", channel_id="robot-code", chat_id="user-1")
    _build_live_state(channel, session_key, "card-1")

    await channel.send(
        OutboundMessage(
            session_key=session_key,
            content=content,
            metadata={"card_biz_id": "card-1"},
        )
    )

    request_json = channel._http.put.await_args.kwargs["json"]
    card_data = json.loads(request_json["cardData"])
    markdown_blocks = [
        block["text"] for block in card_data["contents"] if block.get("id") == "content_markdown"
    ]
    normalized_content = channel._normalize_text_for_sample_text(content)
    expected_chunks = channel._split_dingtalk_content(normalized_content)
    expected_content = "\n\n".join(expected_chunks)

    assert markdown_blocks == [expected_content]


def test_dingtalk_normalizes_markdown_table_into_bullets_before_sending() -> None:
    channel = DingTalkChannel(
        DingTalkChannelConfig(client_id="robot-code", client_secret="secret"),
        MessageBus(),
    )

    card_data = json.loads(
        channel._build_interactive_card_data(
            """
总结如下：

| 状态码 | 状态含义 | 类型 | 说明 |
|---|---|---|---|
| R | 预订状态 | 基本状态 | 已确认预订，尚未入住 |
| I | 当前在住 | 基本状态 | 已办理入住，正在住店 |
| O | 本日结账 | 基本状态 | 当日已退房并完成结账 |
""".strip()
        )
    )

    contents = [
        block["text"] for block in card_data["contents"] if block.get("id") == "content_markdown"
    ]
    joined = "\n\n".join(contents)
    assert "|---" not in joined
    assert "状态码: R；状态含义: 预订状态；类型: 基本状态；说明: 已确认预订，尚未入住" in joined
    assert "状态码: I；状态含义: 当前在住；类型: 基本状态；说明: 已办理入住，正在住店" in joined


@pytest.mark.asyncio
async def test_dingtalk_progress_events_refresh_existing_card() -> None:
    channel = DingTalkChannel(
        DingTalkChannelConfig(client_id="robot-code", client_secret="secret"),
        MessageBus(),
    )
    channel._http = AsyncMock()
    channel._http.put = AsyncMock(return_value=httpx.Response(200, json={}))
    channel._get_access_token = AsyncMock(return_value="token")
    session_key = SessionKey(type="dingtalk", channel_id="robot-code", chat_id="user-1")
    _build_live_state(channel, session_key, "card-1")

    await channel.send(
        OutboundMessage(
            session_key=session_key,
            content='openviking_search({"query": "XR"})',
            event_type=OutboundEventType.TOOL_CALL,
        )
    )

    request_json = channel._http.put.await_args.kwargs["json"]
    card_data = json.loads(request_json["cardData"])
    text_blocks = [block["text"] for block in card_data["contents"] if block.get("type") == "text"]
    markdown_blocks = [block["text"] for block in card_data["contents"] if block.get("type") == "markdown"]

    assert request_json["cardBizId"] == "card-1"
    assert text_blocks[0] == DINGTALK_PLACEHOLDER_TEXT
    assert any("检索知识库" in block for block in markdown_blocks)


@pytest.mark.asyncio
async def test_dingtalk_on_message_attaches_loading_card_metadata() -> None:
    channel = DingTalkChannel(
        DingTalkChannelConfig(client_id="robot-code", client_secret="secret"),
        MessageBus(),
    )
    session_key = SessionKey(type="dingtalk", channel_id="robot-code", chat_id="user-1")
    state = DingTalkCardState(
        session_key=session_key.safe_name(),
        chat_id="user-1",
        single_chat_receiver=channel._make_single_chat_receiver("user-1"),
        card_biz_id="card-1",
    )
    channel._send_loading_card = AsyncMock(return_value=state)
    channel._handle_message = AsyncMock()

    await channel._on_message("你好", "user-1", "Tester")

    metadata = channel._handle_message.await_args.kwargs["metadata"]
    assert metadata["message_id"] == "card-1"
    assert metadata["card_biz_id"] == "card-1"
