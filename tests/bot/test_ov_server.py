# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for OpenViking server inline image materialization."""

from types import SimpleNamespace
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


@pytest.mark.asyncio
async def test_materialize_inline_image_refs_supports_raw_viking_image_refs() -> None:
    client = object.__new__(VikingClient)
    client._resolve_image_asset_uri = AsyncMock()
    client.stat = AsyncMock(return_value={"isDir": False, "name": "image10.png"})
    client._export_image_uris_for_send = AsyncMock(return_value=["![image10](send://image10.png)"])

    rendered = await client.materialize_inline_image_refs(
        "架构说明\n![部署图](viking://resources/demo/_images/image10.png)",
        "viking://resources/demo/manual.md",
    )

    assert rendered == "架构说明\n![部署图](send://image10.png)"
    client._resolve_image_asset_uri.assert_not_called()
    client.stat.assert_awaited_once_with("viking://resources/demo/_images/image10.png")


@pytest.mark.asyncio
async def test_materialize_inline_image_refs_converts_http_includepicture_and_strips_controls() -> None:
    client = object.__new__(VikingClient)
    client._resolve_image_asset_uri = AsyncMock()
    client._export_image_uris_for_send = AsyncMock()

    content = (
        "步骤一：\n"
        "\x13 INCLUDEPICTURE \"https://example.com/assets/login.png?token=abc\" \\* MERGEFORMATINET \x14\x01\x15\n"
        "工号\x01：请输入账号\n"
        "\x13 INCLUDEPICTURE \"C:\\\\Users\\\\Admin\\\\Temp\\\\foo.jpg\" \\* MERGEFORMATINET \x14\x01\x15注意：仅示意\n"
    )

    rendered = await client.materialize_inline_image_refs(
        content,
        "viking://resources/demo/manual.md",
    )

    assert "![login](https://example.com/assets/login.png?token=abc)" in rendered
    assert "工号：" in rendered
    assert "C:\\Users\\Admin\\Temp\\foo.jpg" not in rendered
    assert "\x01" not in rendered
    client._resolve_image_asset_uri.assert_not_called()
    client._export_image_uris_for_send.assert_not_called()


@pytest.mark.asyncio
async def test_export_referenced_images_prefers_nearby_human_readable_caption(tmp_path) -> None:
    client = object.__new__(VikingClient)
    client._resolve_image_asset_uri = AsyncMock(
        return_value="viking://resources/demo/_images/image10.png"
    )
    client._export_image_uris_to_directory = AsyncMock(
        return_value=[
            {
                "source_uri": "viking://resources/demo/_images/image10.png",
                "local_path": str(tmp_path / "image10.png"),
                "caption": "image10",
                "page_hint": "",
            }
        ]
    )

    exported = await client.export_referenced_images(
        "系统说明\n![image10](ov-asset://image10.png)\n▲XMS系统部署架构图",
        "viking://resources/demo/manual.md",
        output_dir=tmp_path,
        max_images=2,
    )

    assert exported[0]["caption"] == "XMS系统部署架构图"


@pytest.mark.asyncio
async def test_find_related_image_uris_stops_at_parsed_document_root() -> None:
    client = object.__new__(VikingClient)
    client._resolve_start_directory = AsyncMock(
        return_value="viking://resources/xms-support/01-base.docx"
    )
    client.client = SimpleNamespace(stat=AsyncMock(return_value={}))

    result = await client._find_related_image_uris(
        "viking://resources/xms-support/01-base.docx/01-base_2.md"
    )

    assert result == []
    client.client.stat.assert_awaited_once_with("viking://resources/xms-support/01-base.docx/_images")
