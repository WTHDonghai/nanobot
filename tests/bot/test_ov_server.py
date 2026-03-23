# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for OpenViking server inline image materialization."""

from unittest.mock import AsyncMock

import pytest

from vikingbot.openviking_mount.ov_server import VikingClient


@pytest.mark.asyncio
async def test_materialize_inline_image_refs_raises_when_asset_cannot_be_resolved() -> None:
    client = object.__new__(VikingClient)
    client._resolve_image_asset_uri = AsyncMock(return_value=None)

    with pytest.raises(ValueError, match="Unable to resolve inline image asset 'image1.png'"):
        await client.materialize_inline_image_refs(
            "请按图操作：\n![image1](ov-asset://image1.png)",
            "viking://resources/demo/manual.md",
        )


@pytest.mark.asyncio
async def test_materialize_inline_image_refs_raises_when_export_does_not_produce_send_ref() -> None:
    client = object.__new__(VikingClient)
    client._resolve_image_asset_uri = AsyncMock(
        return_value="viking://resources/demo/_images/image1.png"
    )
    client._export_image_uris_for_send = AsyncMock(return_value=["![image1](http://example.com/foo.png)"])

    with pytest.raises(ValueError, match="did not produce a send:// reference"):
        await client.materialize_inline_image_refs(
            "请按图操作：\n![image1](ov-asset://image1.png)",
            "viking://resources/demo/manual.md",
        )
