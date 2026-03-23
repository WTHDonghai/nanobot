# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for DingTalk channel image rewriting."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from vikingbot.bus.events import OutboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.channels.dingtalk import DINGTALK_TEXT_SOFT_LIMIT, DingTalkChannel
from vikingbot.config.schema import DingTalkChannelConfig, SessionKey


@pytest.mark.asyncio
async def test_dingtalk_send_rewrites_send_images_to_media_ids(monkeypatch) -> None:
    channel = DingTalkChannel(
        DingTalkChannelConfig(client_id="robot-code", client_secret="secret"),
        MessageBus(),
    )
    channel._http = AsyncMock()
    channel._http.post = AsyncMock(return_value=httpx.Response(200, json={}))
    channel._get_access_token = AsyncMock(return_value="token")
    channel._parse_data_uri = AsyncMock(return_value=(False, b"image-bytes"))
    monkeypatch.setattr(
        channel,
        "_upload_image_to_dingtalk",
        AsyncMock(return_value="media-id-123"),
    )

    await channel.send(
        OutboundMessage(
            session_key=SessionKey(type="dingtalk", channel_id="robot-code", chat_id="user-1"),
            content="请参考下图：\n![流程图](send://workflow.png)",
        )
    )

    request_json = channel._http.post.await_args.kwargs["json"]
    msg_param = json.loads(request_json["msgParam"])

    assert request_json["msgKey"] == "sampleMarkdown"
    assert "media-id-123" in msg_param["text"]
    assert "send://workflow.png" not in msg_param["text"]


@pytest.mark.asyncio
async def test_dingtalk_send_aborts_when_inline_image_upload_fails(monkeypatch) -> None:
    channel = DingTalkChannel(
        DingTalkChannelConfig(client_id="robot-code", client_secret="secret"),
        MessageBus(),
    )
    channel._http = AsyncMock()
    channel._http.post = AsyncMock(return_value=httpx.Response(200, json={}))
    channel._get_access_token = AsyncMock(return_value="token")
    channel._parse_data_uri = AsyncMock(return_value=(False, b"image-bytes"))
    monkeypatch.setattr(
        channel,
        "_upload_image_to_dingtalk",
        AsyncMock(side_effect=RuntimeError("upload failed")),
    )

    await channel.send(
        OutboundMessage(
            session_key=SessionKey(type="dingtalk", channel_id="robot-code", chat_id="user-1"),
            content="请参考下图：\n![流程图](send://workflow.png)",
        )
    )

    channel._http.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_dingtalk_send_splits_long_plain_text_into_sample_text_messages() -> None:
    channel = DingTalkChannel(
        DingTalkChannelConfig(client_id="robot-code", client_secret="secret"),
        MessageBus(),
    )
    channel._http = AsyncMock()
    channel._http.post = AsyncMock(return_value=httpx.Response(200, json={}))
    channel._get_access_token = AsyncMock(return_value="token")

    paragraph_one = "这是一段较长的纯文本回复，用来验证钉钉长消息的安全分段发送能力。" * 3
    paragraph_two = "这里继续补充第二段说明，确认纯文本不会继续走 markdown 模板导致展示裁切。" * 3
    paragraph_three = "最后一段用于确保超过单条建议长度后，渠道会拆成多条 sampleText 消息发送。" * 3
    content = "\n\n".join([paragraph_one, paragraph_two, paragraph_three])

    await channel.send(
        OutboundMessage(
            session_key=SessionKey(type="dingtalk", channel_id="robot-code", chat_id="user-1"),
            content=content,
        )
    )

    assert channel._http.post.await_count >= 2

    chunks: list[str] = []
    for call in channel._http.post.await_args_list:
        request_json = call.kwargs["json"]
        assert request_json["msgKey"] == "sampleText"
        msg_param = json.loads(request_json["msgParam"])
        chunks.append(msg_param["content"])
        assert channel._display_units(msg_param["content"]) <= DINGTALK_TEXT_SOFT_LIMIT

    assert "\n\n".join(chunks) == content


def test_dingtalk_normalizes_markdown_table_into_bullets_before_sending() -> None:
    channel = DingTalkChannel(
        DingTalkChannelConfig(client_id="robot-code", client_secret="secret"),
        MessageBus(),
    )

    payloads = channel._build_message_payloads(
        """
总结如下：

| 状态码 | 状态含义 | 类型 | 说明 |
|---|---|---|---|
| R | 预订状态 | 基本状态 | 已确认预订，尚未入住 |
| I | 当前在住 | 基本状态 | 已办理入住，正在住店 |
| O | 本日结账 | 基本状态 | 当日已退房并完成结账 |
""".strip()
    )

    assert payloads
    assert all(payload["msgKey"] == "sampleText" for payload in payloads)
    joined = "\n\n".join(json.loads(payload["msgParam"])["content"] for payload in payloads)
    assert "|---" not in joined
    assert "状态码: R；状态含义: 预订状态；类型: 基本状态；说明: 已确认预订，尚未入住" in joined
    assert "状态码: I；状态含义: 当前在住；类型: 基本状态；说明: 已办理入住，正在住店" in joined
