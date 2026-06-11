# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for OpenViking server inline image materialization."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from vikingbot.openviking_mount.ov_server import VikingClient


@pytest.mark.asyncio
async def test_export_image_uris_downloads_in_parallel_and_preserves_order(tmp_path) -> None:
    client = object.__new__(VikingClient)
    started: list[str] = []
    all_started = asyncio.Event()
    image_uris = [
        "viking://resources/demo/_images/page_1.png",
        "viking://resources/demo/_images/page_2.png",
        "viking://resources/demo/_images/page_3.png",
    ]

    async def download_content(uri: str) -> bytes:
        started.append(uri)
        if len(started) == len(image_uris):
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=0.5)
        return uri.encode("utf-8")

    client.download_content = AsyncMock(side_effect=download_content)

    exported = await client._export_image_uris_to_directory(image_uris, tmp_path)

    assert started == image_uris
    assert [item["source_uri"] for item in exported] == image_uris


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
    client.stat.assert_awaited_once_with(
        "viking://resources/demo/_images/image10.png",
        quiet=True,
        cache_missing=True,
    )


@pytest.mark.asyncio
async def test_materialize_inline_image_refs_processes_distinct_images_in_parallel() -> None:
    client = object.__new__(VikingClient)
    started: list[str] = []
    all_started = asyncio.Event()

    async def export_image(image_uris: list[str]) -> list[str]:
        image_uri = image_uris[0]
        started.append(image_uri)
        if len(started) == 2:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=0.5)
        image_name = image_uri.rsplit("/", 1)[-1]
        return [f"![{image_name}](send://{image_name})"]

    client._resolve_image_asset_uri = AsyncMock(
        side_effect=[
            "viking://resources/demo/_images/image1.png",
            "viking://resources/demo/_images/image2.png",
        ]
    )
    client._export_image_uris_for_send = AsyncMock(side_effect=export_image)

    rendered = await client.materialize_inline_image_refs(
        "![一](ov-asset://image1.png)\n![二](ov-asset://image2.png)",
        "viking://resources/demo/manual.md",
    )

    assert started == [
        "viking://resources/demo/_images/image1.png",
        "viking://resources/demo/_images/image2.png",
    ]
    assert rendered == "![一](send://image1.png)\n![二](send://image2.png)"


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
async def test_materialize_preview_image_refs_resolves_relative_images() -> None:
    client = object.__new__(VikingClient)
    client._resolve_start_directory = AsyncMock(return_value="viking://resources/demo/manual")
    client.stat = AsyncMock(return_value={"isDir": False, "name": "login.png"})
    client._export_image_uris_for_send = AsyncMock(return_value=["![login](send://login.png)"])

    rendered = await client.materialize_preview_image_refs(
        "步骤一\n![登录图](./_images/login.png)",
        "viking://resources/demo/manual/page_1.md",
    )

    assert rendered == "步骤一\n![登录图](send://login.png)"
    client.stat.assert_awaited_once_with(
        "viking://resources/demo/manual/_images/login.png",
        quiet=True,
        cache_missing=True,
    )
    client._export_image_uris_for_send.assert_awaited_once_with(
        ["viking://resources/demo/manual/_images/login.png"]
    )


@pytest.mark.asyncio
async def test_materialize_preview_image_refs_keeps_other_images_when_one_fails() -> None:
    client = object.__new__(VikingClient)
    client._resolve_start_directory = AsyncMock(return_value="viking://resources/demo/manual")
    client.stat = AsyncMock(side_effect=[{}, {"isDir": False, "name": "ok.png"}])
    client._export_image_uris_for_send = AsyncMock(return_value=["![ok](send://ok.png)"])

    rendered = await client.materialize_preview_image_refs(
        "![缺失](./_images/missing.png)\n![可用](./_images/ok.png)",
        "viking://resources/demo/manual/page_1.md",
    )

    assert "![缺失](./_images/missing.png)" in rendered
    assert "![可用](send://ok.png)" in rendered
    client._export_image_uris_for_send.assert_awaited_once_with(
        ["viking://resources/demo/manual/_images/ok.png"]
    )


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


@pytest.mark.asyncio
async def test_resolve_image_assets_uses_cached_image_directory_listing() -> None:
    client = object.__new__(VikingClient)
    client._resolve_start_directory = AsyncMock(return_value="viking://resources/demo/manual")
    client.client = SimpleNamespace(
        stat=AsyncMock(
            side_effect=[
                {"isDir": True, "name": "_images"},
                {},
                {},
            ]
        )
    )
    client.list_resources = AsyncMock(
        return_value=[
            {
                "uri": "viking://resources/demo/manual/_images/image2.png",
                "name": "image2.png",
                "isDir": False,
            }
        ]
    )

    missing = await client._resolve_image_asset_uri(
        "viking://resources/demo/manual/page_1.md",
        "image1.png",
    )
    found = await client._resolve_image_asset_uri(
        "viking://resources/demo/manual/page_1.md",
        "image2.png",
    )

    assert missing is None
    assert found == "viking://resources/demo/manual/_images/image2.png"
    assert client.client.stat.await_args_list[0].args == (
        "viking://resources/demo/manual/_images",
    )
    assert client.client.stat.await_count == 3
    client.list_resources.assert_awaited_once_with(
        path="viking://resources/demo/manual/_images",
        recursive=False,
    )


@pytest.mark.asyncio
async def test_resolve_missing_image_directory_is_cached() -> None:
    client = object.__new__(VikingClient)
    client._resolve_start_directory = AsyncMock(return_value="viking://resources/demo/manual")
    client.client = SimpleNamespace(stat=AsyncMock(return_value={}))
    client.list_resources = AsyncMock()

    first = await client._resolve_image_asset_uri(
        "viking://resources/demo/manual/page_1.md",
        "image1.png",
    )
    second = await client._resolve_image_asset_uri(
        "viking://resources/demo/manual/page_1.md",
        "image2.png",
    )

    assert first is None
    assert second is None
    assert client.client.stat.await_args_list[0].args == (
        "viking://resources/demo/manual/_images",
    )
    assert client.client.stat.await_count == 3
    client.list_resources.assert_not_called()


@pytest.mark.asyncio
async def test_missing_image_directory_cache_expires(monkeypatch) -> None:
    client = object.__new__(VikingClient)
    client.client = SimpleNamespace(
        stat=AsyncMock(
            side_effect=[
                {},
                {"isDir": True, "name": "_images"},
            ]
        )
    )
    client.list_resources = AsyncMock(return_value=[])
    current_time = 1000.0

    monkeypatch.setattr(
        "vikingbot.openviking_mount.ov_server.time.monotonic",
        lambda: current_time,
    )

    first = await client._list_image_dir_entries("viking://resources/demo/_images")
    cached = await client._list_image_dir_entries("viking://resources/demo/_images")
    current_time += 31.0
    expired = await client._list_image_dir_entries("viking://resources/demo/_images")

    assert first is None
    assert cached is None
    assert expired == []
    assert client.client.stat.await_count == 2
    client.list_resources.assert_awaited_once_with(
        path="viking://resources/demo/_images",
        recursive=False,
    )


@pytest.mark.asyncio
async def test_concurrent_missing_image_directory_stat_is_deduplicated() -> None:
    client = object.__new__(VikingClient)
    client._resolve_start_directory = AsyncMock(return_value="viking://resources/demo/manual")
    started = 0
    release = asyncio.Event()

    async def stat_once(uri: str):
        nonlocal started
        started += 1
        await release.wait()
        return {}

    client.client = SimpleNamespace(stat=AsyncMock(side_effect=stat_once))
    client.list_resources = AsyncMock()

    tasks = [
        asyncio.create_task(
            client._resolve_image_asset_uri(
                "viking://resources/demo/manual/page_1.md",
                f"image{index}.png",
            )
        )
        for index in range(4)
    ]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks)

    assert results == [None, None, None, None]
    assert started == 3
    assert client.client.stat.await_count == 3
    assert [call.args[0] for call in client.client.stat.await_args_list] == [
        "viking://resources/demo/manual/_images",
        "viking://resources/demo/_images",
        "viking://resources/_images",
    ]
    client.list_resources.assert_not_called()
