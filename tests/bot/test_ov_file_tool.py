# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for OpenViking bot file tools."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vikingbot.agent.tools.base import ToolContext
from vikingbot.agent.tools.ov_file import VikingGrepTool, VikingReadTool, VikingSearchTool
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
async def test_viking_read_tool_keeps_http_inline_images_without_probing_for_related_dirs() -> None:
    tool = VikingReadTool()
    mock_client = AsyncMock()
    mock_client.stat.return_value = {"isDir": False, "name": "manual.md"}
    mock_client.read_content.return_value = "步骤一\nINCLUDEPICTURE"
    mock_client.materialize_inline_image_refs.return_value = (
        "步骤一\n![login](https://example.com/assets/login.png)"
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

    assert result == "步骤一\n![login](https://example.com/assets/login.png)"
    mock_client.export_related_images_for_send.assert_not_called()


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
async def test_viking_read_tool_normalizes_summary_uri_for_non_read_levels() -> None:
    tool = VikingReadTool()
    mock_client = AsyncMock()
    mock_client.read_content.return_value = "目录摘要"
    tool._get_client = AsyncMock(return_value=mock_client)

    result = await tool.execute(
        ToolContext(
            session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user"),
            workspace_id="workspace-1",
        ),
        uri="viking://resources/.abstract.md",
        level="abstract",
    )

    assert result == "目录摘要"
    mock_client.read_content.assert_awaited_once_with(
        "viking://resources",
        level="abstract",
    )


@pytest.mark.asyncio
async def test_viking_read_tool_marks_generic_scope_summary_as_non_evidence() -> None:
    tool = VikingReadTool()
    mock_client = AsyncMock()
    mock_client.read_content.return_value = "Resources scope summary."
    tool._get_client = AsyncMock(return_value=mock_client)

    result = await tool.execute(
        ToolContext(
            session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user"),
            workspace_id="workspace-1",
        ),
        uri="viking://resources/.abstract.md",
        level="read",
    )

    assert "这是作用域级摘要" in result
    assert "不要直接根据这段摘要回答用户问题" in result
    assert "Resources scope summary." in result
    mock_client.read_content.assert_awaited_once_with(
        "viking://resources/.abstract.md",
        level="read",
    )


@pytest.mark.asyncio
async def test_viking_read_tool_marks_section_summary_as_non_evidence_and_lists_candidates() -> None:
    tool = VikingReadTool()
    mock_client = AsyncMock()
    mock_client.resolve_read_uri.return_value = (
        None,
        [
            "viking://resources/03-reception/第四节_散客登记/散客登记.md",
            "viking://resources/03-reception/第四节_散客登记/散客登记_2.md",
        ],
    )
    mock_client.read_content.return_value = "散客登记相关摘要"
    tool._get_client = AsyncMock(return_value=mock_client)

    result = await tool.execute(
        ToolContext(
            session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user"),
            workspace_id="workspace-1",
        ),
        uri="viking://resources/03-reception/第四节_散客登记/.abstract.md",
        level="read",
    )

    assert "这是目录/章节摘要，不是具体文档正文" in result
    assert "不要直接根据这段摘要回答用户问题" in result
    assert "散客登记.md" in result
    assert "摘要内容（仅供定位，不可直接作答）" in result
    mock_client.resolve_read_uri.assert_awaited_once_with(
        "viking://resources/03-reception/第四节_散客登记"
    )


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

    assert result.index("Documents:") < result.index("Image assets:")
    assert "viking://resources/demo/房价码设置.md" in result
    assert "viking://resources/demo/_images/image14.png" in result
    assert "read the document with include_images=true" in result


@pytest.mark.asyncio
async def test_viking_search_tool_forwards_limit() -> None:
    tool = VikingSearchTool()
    mock_client = AsyncMock()
    mock_client.search.return_value = {
        "total": 1,
        "query": "宾客状态",
        "limit": 7,
        "resources": [
            {
                "uri": "viking://resources/demo/宾客状态.md",
                "abstract": "宾客状态说明。",
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
        query="宾客状态",
        target_uri="viking://resources/demo/",
        limit=7,
    )

    mock_client.search.assert_awaited_once_with(
        "宾客状态", target_uri="viking://resources/demo/", limit=7
    )
    assert "Requested limit: 7" in result


@pytest.mark.asyncio
async def test_viking_search_tool_prioritizes_image_assets_for_image_focused_queries() -> None:
    tool = VikingSearchTool()
    mock_client = AsyncMock()
    mock_client.search.return_value = {
        "total": 2,
        "query": "部署架构截图",
        "resources": [
            {
                "uri": "viking://resources/demo/部署说明.md",
                "abstract": "部署说明与截图索引。",
                "match_reason": "Matched by content",
            },
            {
                "uri": "viking://resources/demo/_images/deploy_overview.png",
                "abstract": "部署架构截图。",
                "match_reason": "Matched by visible text",
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
        query="部署架构截图",
        target_uri="viking://resources/demo/",
    )

    assert result.index("Image assets:") < result.index("Documents:")
    assert result.index("viking://resources/demo/_images/deploy_overview.png") < result.index(
        "viking://resources/demo/部署说明.md"
    )
    assert "read a matched image asset URI directly" in result


@pytest.mark.asyncio
async def test_viking_search_tool_demotes_generic_scope_summaries() -> None:
    tool = VikingSearchTool()
    mock_client = AsyncMock()
    mock_client.search.return_value = {
        "total": 2,
        "query": "XMS 系统基础",
        "resources": [
            {
                "uri": "viking://resources/.abstract.md",
                "abstract": "Resources scope summary",
                "score": 0.95,
            },
            {
                "uri": "viking://resources/xms-support/01-base.docx",
                "abstract": "XMS foundational documentation",
                "score": 0.72,
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
        query="XMS 系统基础",
        target_uri="viking://resources/",
    )

    assert result.index("viking://resources/xms-support/01-base.docx") < result.index(
        "viking://resources/.abstract.md"
    )


@pytest.mark.asyncio
async def test_viking_search_tool_warns_when_only_generic_scope_summaries_are_found() -> None:
    tool = VikingSearchTool()
    mock_client = AsyncMock()
    mock_client.search.return_value = {
        "total": 1,
        "query": "宾客有哪些状态",
        "resources": [
            {
                "uri": "viking://resources/.abstract.md",
                "abstract": "Resources scope summary",
                "score": 0.95,
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
        query="宾客有哪些状态",
        target_uri="viking://resources/",
    )

    assert "Generic scope summary only. Not a concrete document." in result
    assert "only summary matches were found" in result
    assert "use openviking_glob to locate concrete files" in result


@pytest.mark.asyncio
async def test_viking_search_tool_demotes_non_generic_summary_uris_below_concrete_docs() -> None:
    tool = VikingSearchTool()
    mock_client = AsyncMock()
    mock_client.search.return_value = {
        "total": 2,
        "query": "接待流程",
        "resources": [
            {
                "uri": "viking://resources/03-reception/.abstract.md",
                "abstract": "接待流程目录摘要",
                "score": 0.95,
            },
            {
                "uri": "viking://resources/03-reception/总览/接待流程总览.md",
                "abstract": "接待流程总览正文",
                "score": 0.72,
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
        query="接待流程",
        target_uri="viking://resources/",
    )

    assert result.index("viking://resources/03-reception/总览/接待流程总览.md") < result.index(
        "viking://resources/03-reception/.abstract.md"
    )
    assert "Summary only. Not a concrete document." in result


@pytest.mark.asyncio
async def test_viking_search_tool_formats_findresult_like_objects_from_admin_search() -> None:
    tool = VikingSearchTool()
    mock_client = SimpleNamespace(
        _matched_context_to_dict=lambda item: {
            "uri": getattr(item, "uri", ""),
            "abstract": getattr(item, "abstract", ""),
            "match_reason": getattr(item, "match_reason", ""),
            "score": getattr(item, "score", 0.0),
        },
        search=AsyncMock(
            return_value=SimpleNamespace(
                resources=[
                    SimpleNamespace(
                        uri="viking://resources/demo/manual.md",
                        abstract="manual",
                        match_reason="Matched by content",
                        score=0.88,
                    )
                ],
                memories=[],
                skills=[],
                total=1,
            )
        ),
    )
    tool._get_client = AsyncMock(return_value=mock_client)

    result = await tool.execute(
        ToolContext(
            session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user"),
            workspace_id="workspace-1",
        ),
        query="manual",
        target_uri="viking://resources/demo/",
    )

    assert result.startswith("OpenViking search query: manual")
    assert "FindResult(" not in result
    assert "viking://resources/demo/manual.md" in result


@pytest.mark.asyncio
async def test_viking_grep_tool_reports_actual_match_count_when_backend_count_is_zero() -> None:
    tool = VikingGrepTool()
    mock_client = AsyncMock()
    mock_client.grep.return_value = {
        "count": 0,
        "matches": [
            {
                "uri": "viking://resources/xms-support/01-base/01-base_1.md",
                "line": 98,
                "content": "**2.1宾客状态**",
            }
        ],
    }
    tool._get_client = AsyncMock(return_value=mock_client)

    result = await tool.execute(
        ToolContext(
            session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user"),
            workspace_id="workspace-1",
        ),
        uri="viking://resources/xms-support/01-base/",
        pattern="宾客.*状态",
    )

    assert result.startswith("Found 1 match across 1 pattern:")
    assert "**2.1宾客状态**" in result


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
