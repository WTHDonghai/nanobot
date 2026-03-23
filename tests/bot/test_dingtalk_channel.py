# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for DingTalk channel image rewriting."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from vikingbot.bus.events import OutboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.channels.dingtalk import DingTalkChannel
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
