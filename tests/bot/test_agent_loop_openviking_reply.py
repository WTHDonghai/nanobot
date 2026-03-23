# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for grounded OpenViking image-section replies."""

import asyncio

from vikingbot.agent.loop import AgentLoop


def test_agent_loop_selects_relevant_markdown_section_with_inline_images() -> None:
    content = """
# 8.3酒店EDP维护手册(XMS)

# 酒店EDP维护手册

目录

# 一、系统运行环境安装

当我们第一次在电脑上使用XMS系统时，需要安装该运行环境。

1）打开浏览器，在地址栏输入相应的网址，进入XMS界面。

2）打开帮助说明之后，单击蓝色字体『系统运行环境』。

![image5](send://image5.png)

3）点击『系统运行环境』按钮，弹出运行环境下载界面。

![image6](send://image6.png)

4）双击打开已经下载完成的运行环境。

# 二、房态维护

这里是另一节无关内容。
""".strip()

    selected = AgentLoop._select_relevant_markdown_section("xms 如何下载运行环境", content)

    assert selected is not None
    assert selected.startswith("# 一、系统运行环境安装")
    assert "send://image5.png" in selected
    assert selected.index("系统运行环境") < selected.index("send://image5.png")
    assert "房态维护" not in selected


def test_agent_loop_keeps_grounded_section_available_when_tools_are_mixed() -> None:
    selected = AgentLoop._select_relevant_openviking_section(
        "xms 如何下载运行环境",
        [
            {
                "tool_name": "openviking_read",
                "result": """
# 一、系统运行环境安装

打开帮助说明之后，单击蓝色字体『系统运行环境』。

![image5](send://image5.png)
""".strip(),
            },
            {
                "tool_name": "web_search",
                "result": "extra evidence",
            },
        ],
    )

    assert selected is not None
    assert selected.startswith("# 一、系统运行环境安装")


def test_agent_loop_selects_multiple_sections_for_multi_intent_question() -> None:
    selected_sections = AgentLoop._select_relevant_openviking_sections(
        "XMS如何进行安装？后续还要做什么设置?",
        [
            {
                "tool_name": "openviking_read",
                "result": """
# 一、系统运行环境安装

1）打开帮助说明。

![image5](send://image5.png)

# 二、安装后必须完成的 4 项核心配置

1）先完成基础参数初始化。

![image10](send://image10.png)
""".strip(),
            }
        ],
        draft_reply="""
## 一、XMS 安装流程（含运行环境下载）

## 二、安装后必须完成的 4 项核心配置
""".strip(),
    )

    assert len(selected_sections) == 2
    assert selected_sections[0].startswith("# 一、系统运行环境安装")
    assert selected_sections[1].startswith("# 二、安装后必须完成的 4 项核心配置")


def test_agent_loop_skips_grounded_section_when_multiple_distinct_reads_match() -> None:
    selected = AgentLoop._select_relevant_openviking_section(
        "xms 如何下载运行环境",
        [
            {
                "tool_name": "openviking_read",
                "result": """
# 一、系统运行环境安装

打开帮助说明之后，单击蓝色字体『系统运行环境』。

![image5](send://image5.png)
""".strip(),
            },
            {
                "tool_name": "openviking_read",
                "result": """
# 二、客户端安装

进入客户端下载页。

![image6](send://image6.png)
""".strip(),
            },
        ],
    )

    assert selected is None


def test_agent_loop_prepares_text_block_for_rewrite() -> None:
    cleaned = AgentLoop._prepare_text_block_for_rewrite(
        """
**1.****        ****场景和需求**

1）打开浏览器，在地址栏输入相应的网址，进入XMS界面。

2）在该界面的右下角有一个问号的图标，单击该图标，打开『帮助说明』。
""".strip()
    )

    assert cleaned.startswith("1. 场景和需求")
    assert "1. 打开浏览器" in cleaned
    assert "2. 在该界面的右下角有一个问号的图标" in cleaned
    assert "**" not in cleaned


def test_agent_loop_extracts_structured_section_with_step_bound_images() -> None:
    title, intro_segments, steps = AgentLoop._extract_structured_section(
        """
# 一、系统运行环境安装

当我们第一次在电脑上使用 XMS 系统时，需要先安装运行环境。

1）打开帮助说明。

![image5](send://image5.png)

点击『系统运行环境』按钮。

![image6](send://image6.png)

2）双击已下载的运行环境。
""".strip()
    )

    assert title == "# 一、系统运行环境安装"
    assert AgentLoop._render_segments(intro_segments) == "当我们第一次在电脑上使用 XMS 系统时，需要先安装运行环境。"
    assert len(steps) == 2
    assert steps[0]["number"] == "1"
    assert [segment["type"] for segment in steps[0]["segments"]] == ["text", "image", "text", "image"]
    assert steps[0]["segments"][1]["content"] == "![image5](send://image5.png)"
    assert steps[0]["segments"][3]["content"] == "![image6](send://image6.png)"


def test_agent_loop_extracts_grounded_intro_from_agent_draft() -> None:
    intro = AgentLoop._extract_grounded_intro(
        """
## 系统运行环境安装

1. 打开帮助说明。

如果你是第一次登录 XMS，需要先安装运行环境。
""".strip()
    )

    assert intro == "如果你是第一次登录 XMS，需要先安装运行环境。"


def test_agent_loop_filters_internal_tool_language_from_grounded_intro() -> None:
    intro = AgentLoop._extract_grounded_intro(
        """
根据你明确的技术偏好，我已调取 OpenViking 中最新版部署手册，为你整理如下。

XMS 安装和后续设置可以按下面几部分进行。
""".strip()
    )

    assert intro == "XMS 安装和后续设置可以按下面几部分进行。"


def test_agent_loop_builds_grounded_reply_without_rewriting_step_structure() -> None:
    async def run_case() -> str:
        loop = object.__new__(AgentLoop)
        reply, _usage = await loop._build_rewritten_openviking_reply(
            "xms 如何下载运行环境",
            [
                {
                    "tool_name": "openviking_read",
                    "result": """
# 一、系统运行环境安装

当我们第一次在电脑上使用 XMS 系统时，需要先安装运行环境。

1）打开帮助说明。

![image5](send://image5.png)

点击『系统运行环境』按钮。

![image6](send://image6.png)

2）双击已下载的运行环境。
""".strip(),
                }
            ],
            draft_reply="如果你是第一次登录 XMS，需要先安装运行环境。",
        )
        return reply or ""

    reply = asyncio.run(run_case())

    assert reply.startswith("如果你是第一次登录 XMS，需要先安装运行环境。")
    assert "## 系统运行环境安装" in reply
    assert "当我们第一次在电脑上使用 XMS 系统时，需要先安装运行环境。" in reply
    assert reply.index("1. 打开帮助说明。") < reply.index("send://image5.png") < reply.index(
        "点击『系统运行环境』按钮。"
    )
    assert reply.index("点击『系统运行环境』按钮。") < reply.index("send://image6.png") < reply.index(
        "2. 双击已下载的运行环境。"
    )
    assert "根据资料" not in reply


def test_agent_loop_builds_multi_section_grounded_reply_and_omits_internal_intro() -> None:
    async def run_case() -> str:
        loop = object.__new__(AgentLoop)
        reply, _usage = await loop._build_rewritten_openviking_reply(
            "XMS如何进行安装？后续还要做什么设置?",
            [
                {
                    "tool_name": "openviking_read",
                    "result": """
# 一、系统运行环境安装

1）打开帮助说明。

![image5](send://image5.png)

# 二、安装后必须完成的 4 项核心配置

1）先完成基础参数初始化。

![image10](send://image10.png)
""".strip(),
                }
            ],
            draft_reply="""
根据你明确的技术偏好，我已调取 OpenViking 中最新版部署手册，为你整理如下。

XMS 安装和后续设置可以按下面几部分进行。

## 一、XMS 安装流程（含运行环境下载）

## 二、安装后必须完成的 4 项核心配置
""".strip(),
        )
        return reply or ""

    reply = asyncio.run(run_case())

    assert reply.startswith("XMS 安装和后续设置可以按下面几部分进行。")
    assert "OpenViking" not in reply
    assert "## 系统运行环境安装" in reply
    assert "## 安装后必须完成的 4 项核心配置" in reply
    assert "send://image5.png" in reply
    assert "send://image10.png" in reply
