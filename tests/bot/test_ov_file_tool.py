# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for OpenViking bot file tools."""

from unittest.mock import AsyncMock

import pytest

from vikingbot.agent.tools.base import ToolContext
from vikingbot.agent.tools.ov_file import VikingReadTool, VikingSearchTool
from vikingbot.config.schema import SessionKey


@pytest.mark.asyncio
async def test_viking_read_tool_appends_sendable_images() -> None:
    tool = VikingReadTool()
    mock_client = AsyncMock()
    mock_client.stat.return_value = {"isDir": False, "name": "manual.md"}
    mock_client.read_content.return_value = "文档正文"
    mock_client.export_related_images_for_send.return_value = [
        "![figure_1](send://figure_1.png)",
        "![figure_2](send://figure_2.png)",
    ]
    tool._get_client = AsyncMock(return_value=mock_client)

    result = await tool.execute(
        ToolContext(
            session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user"),
            workspace_id="workspace-1",
        ),
        uri="viking://resources/demo/manual.md",
        level="read",
    )

    assert "文档正文" in result
    assert "send://figure_1.png" in result
    assert "Markdown 图片行" in result


@pytest.mark.asyncio
async def test_viking_read_tool_returns_sendable_image_for_png_uri() -> None:
    tool = VikingReadTool()
    mock_client = AsyncMock()
    mock_client.stat.return_value = {"isDir": False, "name": "figure_1.png"}
    mock_client.export_uri_for_send.return_value = ["![figure_1](send://figure_1.png)"]
    tool._get_client = AsyncMock(return_value=mock_client)

    result = await tool.execute(
        ToolContext(
            session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user"),
            workspace_id="workspace-1",
        ),
        uri="viking://resources/demo/_images/figure_1.png",
        level="read",
    )

    assert "send://figure_1.png" in result
    assert "原样保留" in result
    mock_client.read_content.assert_not_called()


@pytest.mark.asyncio
async def test_viking_read_tool_keeps_inline_image_positions() -> None:
    tool = VikingReadTool()
    mock_client = AsyncMock()
    mock_client.stat.return_value = {"isDir": False, "name": "manual.md"}
    mock_client.export_uri_for_send.return_value = []
    mock_client.read_content.return_value = (
        "先看步骤一。\n![figure_1](ov-asset://image1.png)\n然后继续步骤二。"
    )
    mock_client.materialize_inline_image_refs.return_value = (
        "先看步骤一。\n![figure_1](send://figure_1.png)\n然后继续步骤二。"
    )
    tool._get_client = AsyncMock(return_value=mock_client)

    result = await tool.execute(
        ToolContext(
            session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user"),
            workspace_id="workspace-1",
        ),
        uri="viking://resources/demo/manual.md",
        level="read",
    )

    assert result == "先看步骤一。\n![figure_1](send://figure_1.png)\n然后继续步骤二。"


@pytest.mark.asyncio
async def test_viking_read_tool_resolves_directory_to_named_text_leaf() -> None:
    tool = VikingReadTool()
    mock_client = AsyncMock()
    mock_client.stat.return_value = {"isDir": True, "name": "manual"}
    mock_client.resolve_read_uri.return_value = (
        "viking://resources/demo/manual/manual.md",
        ["viking://resources/demo/manual/manual.md"],
    )
    mock_client.read_content.return_value = "目录正文"
    mock_client.materialize_inline_image_refs.return_value = "目录正文"
    mock_client.export_related_images_for_send.return_value = [
        "![figure_1](send://figure_1.png)",
    ]
    tool._get_client = AsyncMock(return_value=mock_client)

    result = await tool.execute(
        ToolContext(
            session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user"),
            workspace_id="workspace-1",
        ),
        uri="viking://resources/demo/manual",
        level="read",
    )

    assert "目录正文" in result
    assert "send://figure_1.png" in result
    mock_client.read_content.assert_awaited_once_with(
        "viking://resources/demo/manual/manual.md",
        level="read",
    )
    mock_client.export_related_images_for_send.assert_awaited_once_with(
        "viking://resources/demo/manual/manual.md",
        max_images=4,
    )


@pytest.mark.asyncio
async def test_viking_read_tool_requires_explicit_text_leaf_for_ambiguous_directory() -> None:
    tool = VikingReadTool()
    mock_client = AsyncMock()
    mock_client.stat.return_value = {"isDir": True, "name": "manual"}
    mock_client.resolve_read_uri.return_value = (
        None,
        [
            "viking://resources/demo/manual/section_1.md",
            "viking://resources/demo/manual/section_2.md",
        ],
    )
    tool._get_client = AsyncMock(return_value=mock_client)

    result = await tool.execute(
        ToolContext(
            session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user"),
            workspace_id="workspace-1",
        ),
        uri="viking://resources/demo/manual",
        level="read",
    )

    assert "不能直接执行 level='read'" in result
    assert "section_1.md" in result
    mock_client.read_content.assert_not_called()


@pytest.mark.asyncio
async def test_viking_search_tool_prioritizes_documents_over_image_assets() -> None:
    tool = VikingSearchTool()
    mock_client = AsyncMock()
    mock_client.search.return_value = {
        "total": 2,
        "query": "房价码 设置",
        "resources": [
            {
                "uri": "viking://resources/demo/_images/image14.png",
                "abstract": "房价码配置界面截图",
                "match_reason": "Matched by title",
            },
            {
                "uri": "viking://resources/demo/房价码设置.md",
                "abstract": "房价码设置步骤说明，包含对应截图。",
                "match_reason": "Matched by content",
            },
        ],
        "memories": [],
        "skills": [],
    }
    tool._get_client = AsyncMock(return_value=mock_client)

    result = await tool.execute(
        ToolContext(
            session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user"),
            workspace_id="workspace-1",
        ),
        query="房价码 设置",
        target_uri="viking://resources/demo/",
    )

    assert result.index("viking://resources/demo/房价码设置.md") < result.index(
        "Related image assets:"
    )
    assert "viking://resources/demo/_images/image14.png" not in result
    assert "only use the returned Markdown image lines" in result


@pytest.mark.asyncio
async def test_viking_search_tool_guides_image_only_results_to_image_reader() -> None:
    tool = VikingSearchTool()
    mock_client = AsyncMock()
    mock_client.search.return_value = {
        "total": 1,
        "query": "房价码界面截图",
        "resources": [
            {
                "uri": "viking://resources/demo/_images/image14.png",
                "abstract": "房价码配置界面截图",
                "match_reason": "Matched by title",
            },
        ],
        "memories": [],
        "skills": [],
    }
    tool._get_client = AsyncMock(return_value=mock_client)

    result = await tool.execute(
        ToolContext(
            session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user"),
            workspace_id="workspace-1",
        ),
        query="房价码界面截图",
        target_uri="viking://resources/demo/",
    )

    assert "most relevant image URI shown above" in result
    assert "most relevant document URI" not in result
