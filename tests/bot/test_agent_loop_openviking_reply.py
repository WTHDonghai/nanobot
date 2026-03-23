# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for grounded OpenViking image-section replies."""

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from vikingbot.agent.loop import AgentLoop
from vikingbot.bus.events import InboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.config.schema import CapabilityProfile, Config, SessionKey
from vikingbot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class StubProvider(LLMProvider):
    """Minimal provider stub for grounded-reply tests."""

    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.calls = []

    async def chat(
        self,
        messages,
        tools=None,
        tool_choice=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        session_id=None,
    ):
        self.calls.append(
            {
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "session_id": session_id,
            }
        )
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "stub-model"


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


def test_agent_loop_treats_http_markdown_images_as_grounded_sections() -> None:
    selected = AgentLoop._select_relevant_openviking_section(
        "如何进行授权登录",
        [
            {
                "tool_name": "openviking_read",
                "result": """
# 3.1授权登录

（1）在浏览器中输入系统的域名或 IP 地址。

![login](https://example.com/assets/login.png)
""".strip(),
            }
        ],
    )

    assert selected is not None
    assert selected.startswith("# 3.1授权登录")
    assert "https://example.com/assets/login.png" in selected


def test_agent_loop_selects_numbered_image_section_from_plain_text_doc() -> None:
    selected = AgentLoop._select_relevant_openviking_section(
        "向我介绍扫码登录",
        [
            {
                "tool_name": "openviking_read",
                "result": """
3.1授权登录
（1）输入工号和密码。

3.2.1扫码登录
（1）进入登录界面选择其他登录方式。
![scan](https://example.com/assets/scan-login.png)
（2）使用微信扫码登录。

3.2.2 AD域登录
通过平台设置配置 AD 域登录。
""".strip(),
            }
        ],
    )

    assert selected is not None
    assert selected.startswith("# 3.2.1扫码登录")
    assert "scan-login.png" in selected
    assert "3.2.2 AD域登录" not in selected


def test_agent_loop_skips_irrelevant_image_section_when_text_only_match_is_stronger() -> None:
    selected = AgentLoop._select_relevant_openviking_section(
        "向我介绍扫码登录",
        [
            {
                "tool_name": "openviking_read",
                "result": """
3.1授权登录
（1）输入工号和密码。

3.2.1扫码登录
（1）进入登录界面选择其他登录方式。
（2）使用微信扫码登录。

3.4更换工号和锁屏
在需要更换工号登录时，可以使用更换工号功能。
![lock](https://example.com/assets/lock-screen.png)
""".strip(),
            }
        ],
    )

    assert selected is None


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


def test_agent_loop_filters_chinese_internal_meta_language_from_grounded_intro() -> None:
    intro = AgentLoop._extract_grounded_intro(
        """
已从 01-base_2.md 中获取到 XMS 系统官方定义的宾客状态码完整列表及流转关系，信息明确、权威，可直接用于回答。

我已确认该内容来自 XMS 基础手册第 2 章“宾客状态”，现在可给出最终答案。

XMS 宾客状态一共分为 9 种，下面按状态码、含义和业务说明整理给你。
""".strip()
    )

    assert intro == "XMS 宾客状态一共分为 9 种，下面按状态码、含义和业务说明整理给你。"


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


def test_agent_loop_collects_relevant_numbered_section_for_text_finalizer() -> None:
    evidence_blocks = AgentLoop._collect_document_evidence_blocks(
        "宾客有哪些状态",
        [
            {
                "tool_name": "openviking_read",
                "args": '{"uri": "viking://resources/xms-support/01-base.docx/01-base_4.md", "level": "read"}',
                "result": "3.15实时房情\n执行开始→查询→实时房情。",
                "execute_success": True,
            },
            {
                "tool_name": "openviking_read",
                "args": '{"uri": "viking://resources/xms-support/01-base.docx/01-base_2.md", "level": "read"}',
                "result": """
2.1宾客状态
主单状态反映一个客人的信息在酒店中所处的状态。
R 预订状态
I 当前在住
Q 问询状态

2.2客房状态
VI 检查房
VC 干净房
""".strip(),
                "execute_success": True,
            },
        ],
    )

    assert evidence_blocks
    assert evidence_blocks[0].startswith("2.1宾客状态")
    assert "2.2客房状态" not in evidence_blocks[0]


def test_agent_loop_collects_bold_numbered_section_from_doc_body_instead_of_toc() -> None:
    evidence_blocks = AgentLoop._collect_document_evidence_blocks(
        "宾客有哪些状态",
        [
            {
                "tool_name": "openviking_read",
                "args": '{"uri": "viking://resources/xms-support/01-base/01-base_1.md", "level": "read"}',
                "result": """
**系统基础**

目录

2.1宾客状态3
2.2客房状态3

**2.1宾客状态**

主单状态反映一个客人的信息在酒店中所处的状态。
R 预订状态
I 当前在住
Q 问询状态
""".strip(),
                "execute_success": True,
            }
        ],
    )

    assert evidence_blocks
    assert evidence_blocks[0].startswith("2.1宾客状态")
    assert "主单状态反映一个客人的信息在酒店中所处的状态" in evidence_blocks[0]
    assert "2.1宾客状态3" not in evidence_blocks[0]


def test_agent_loop_extract_search_result_uris_skips_generic_scope_summaries() -> None:
    uris = AgentLoop._extract_search_result_uris(
        (
            "OpenViking search query: 宾客有哪些状态\n"
            "Total matches: 3\n\n"
            "Resources:\n"
            "1. [document] viking://resources/.abstract.md\n"
            "2. [document] viking://resources/xms-support/01-base/.overview.md\n"
            "3. [document] viking://resources/xms-support/01-base/01-base_1.md\n"
        )
    )

    assert uris == [
        "viking://resources/xms-support/01-base/01-base_1.md",
        "viking://resources/xms-support/01-base/.overview.md",
    ]


def test_agent_loop_has_document_evidence_ignores_generic_scope_summary_reads() -> None:
    assert not AgentLoop._has_document_evidence(
        [
            {
                "tool_name": "openviking_read",
                "args": '{"uri": "viking://resources/.abstract.md", "level": "read"}',
                "result": "Resources scope summary.",
                "execute_success": True,
            }
        ]
    )

    assert AgentLoop._has_document_evidence(
        [
            {
                "tool_name": "openviking_read",
                "args": '{"uri": "viking://resources/xms-support/01-base/01-base_1.md", "level": "read"}',
                "result": "2.1宾客状态\nR 预订状态\nI 当前在住",
                "execute_success": True,
            }
        ]
    )


def test_agent_loop_collect_document_evidence_blocks_ignores_generic_scope_summary_reads() -> None:
    evidence_blocks = AgentLoop._collect_document_evidence_blocks(
        "有哪些登录方式",
        [
            {
                "tool_name": "openviking_read",
                "args": '{"uri": "viking://resources/.overview.md", "level": "read"}',
                "result": "Globally shared resource storage, organized by project/topic.",
                "execute_success": True,
            },
            {
                "tool_name": "openviking_read",
                "args": '{"uri": "viking://resources/xms-support/01-base/01-base_1.md", "level": "read"}',
                "result": """
3.1授权登录
使用工号和密码登录。

3.2其他方式登录
3.2.1扫码登录
使用微信扫码登录。
3.2.2 AD域登录
通过 AD 域进行登录。
""".strip(),
                "execute_success": True,
            },
        ],
    )

    assert evidence_blocks
    assert "project/topic" not in evidence_blocks[0]
    assert "3.2其他方式登录" in evidence_blocks[0]


def test_run_agent_loop_separates_kb_draft_from_final_user_reply() -> None:
    config = Config()
    config.agents.capability_profile = CapabilityProfile.KNOWLEDGE_BASE

    provider = StubProvider(
        [
            LLMResponse(
                content="先读取文档确认细节。",
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="openviking_read",
                        arguments={
                            "uri": "viking://resources/xms-support/01-base.docx/01-base_2.md",
                            "level": "read",
                        },
                        tokens=12,
                    )
                ],
            ),
            LLMResponse(
                content=(
                    "太好了！在 01-base_2.md 中找到了完整的 2.1 宾客状态章节，"
                    "现在我可以基于这份文档给用户最终答案。"
                )
            ),
            LLMResponse(content="宾客状态包括 R、I、O、D、H、N、S、X、Q。"),
        ]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "## Final Answer Contract\n- Final user reply must not mention internal file names.\n",
            encoding="utf-8",
        )
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=config,
            max_iterations=3,
        )
        loop.tools.get_definitions = lambda: []
        loop.tools.execute = AsyncMock(
            return_value="""
2.1宾客状态
主单状态反映一个客人的信息在酒店中所处的状态。
R 预订状态
I 当前在住
O 本日结账
D 昨日结账
H 已结账
N 应到未到的预订
S 临时挂账
X 已被取消的预订
Q 问询状态
""".strip()
        )

        final_content, tools_used, _token_usage = asyncio.run(
            loop._run_agent_loop(
                messages=[{"role": "user", "content": "宾客有哪些状态"}],
                session_key=SessionKey(type="cli", channel_id="default", chat_id="kb-final"),
                publish_events=False,
                user_request="宾客有哪些状态",
                require_document_evidence=True,
            )
        )

    assert len(tools_used) == 1
    assert final_content == "宾客状态包括 R、I、O、D、H、N、S、X、Q。"
    assert "01-base_2.md" not in final_content
    assert provider.calls[-1]["session_id"].endswith("::kb-final")
    assert "SOUL.md" not in final_content
    assert "Final user reply must not mention internal file names." in provider.calls[-1]["messages"][0]["content"]


def test_run_agent_loop_continues_search_until_concrete_kb_evidence_is_ready() -> None:
    config = Config()
    config.agents.capability_profile = CapabilityProfile.KNOWLEDGE_BASE

    provider = StubProvider(
        [
            LLMResponse(content="当前文档中似乎没有明确答案。"),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_1",
                        name="openviking_glob",
                        arguments={"pattern": "**/*.md", "uri": "viking://resources/"},
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="call_2",
                        name="openviking_read",
                        arguments={
                            "uri": "viking://resources/xms-support/01-base/01-base_1.md",
                            "level": "read",
                        },
                        tokens=8,
                    )
                ],
            ),
            LLMResponse(content="我已经拿到具体文档内容。"),
            LLMResponse(content="宾客状态包括 R、I、O、D、H、N、S、X、Q。"),
        ]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "## Final Answer Contract\n- Final user reply must be based on documentation evidence.\n",
            encoding="utf-8",
        )
        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=config,
            max_iterations=5,
        )
        loop.tools.get_definitions = lambda: []
        loop.tools.execute = AsyncMock(
            side_effect=[
                "Found 1 file:\n📄 viking://resources/xms-support/01-base/01-base_1.md",
                """
**2.1宾客状态**

主单状态反映一个客人的信息在酒店中所处的状态。
R 预订状态
I 当前在住
O 本日结账
D 昨日结账
H 已结账
N 应到未到的预订
S 临时挂账
X 已被取消的预订
Q 问询状态
""".strip(),
            ]
        )

        final_content, tools_used, _token_usage = asyncio.run(
            loop._run_agent_loop(
                messages=[{"role": "user", "content": "宾客有哪些状态"}],
                session_key=SessionKey(type="cli", channel_id="default", chat_id="kb-continue"),
                publish_events=False,
                user_request="宾客有哪些状态",
                require_document_evidence=True,
                initial_tools_used=[
                    {
                        "tool_name": "openviking_search",
                        "args": '{"query": "宾客有哪些状态", "target_uri": "viking://resources/"}',
                        "result": (
                            "OpenViking search query: 宾客有哪些状态\n"
                            "Target URI: viking://resources/\n"
                            "Total matches: 1\n\n"
                            "Resources:\n"
                            "1. [document] viking://resources/.abstract.md\n"
                            "   Generic scope summary only. Not a concrete document.\n"
                        ),
                        "execute_success": True,
                    },
                    {
                        "tool_name": "openviking_read",
                        "args": '{"uri": "viking://resources/.abstract.md", "level": "read"}',
                        "result": "这是作用域级摘要，不是具体文档正文。",
                        "execute_success": True,
                    },
                ],
            )
        )

    assert final_content == "宾客状态包括 R、I、O、D、H、N、S、X、Q。"
    assert [tool["tool_name"] for tool in tools_used] == [
        "openviking_search",
        "openviking_read",
        "openviking_glob",
        "openviking_read",
    ]
    assert any(
        isinstance(message.get("content"), str)
        and "The current evidence is still insufficient for a final user answer." in message["content"]
        for call in provider.calls
        for message in call["messages"]
    )


def test_process_message_prefetches_kb_search_and_read_before_first_answer() -> None:
    config = Config()
    config.agents.capability_profile = CapabilityProfile.KNOWLEDGE_BASE

    provider = StubProvider(
        [
            LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        id="route_1",
                        name="route_request",
                        arguments={
                            "label": "knowledge_query",
                            "route": "agent",
                            "confidence": "high",
                            "reason": "asks about xms login workflow",
                        },
                        tokens=10,
                    )
                ],
            ),
            LLMResponse(content="我先基于已有证据整理答案。"),
            LLMResponse(content="授权登录时，先输入域名或IP，再填写工号、密码并选择酒店和模块。"),
        ]
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir)
        (workspace / "SOUL.md").write_text(
            "## Final Answer Contract\n- Final user reply must be based on documentation evidence.\n",
            encoding="utf-8",
        )
        class StubSandboxManager:
            def __init__(self, workspace_path: Path):
                self.config = SimpleNamespace(mode="isolated")
                self.workspace = workspace_path

            def to_workspace_id(self, _session_key):
                return "workspace-1"

            def get_workspace_path(self, _session_key):
                return workspace

            async def get_sandbox(self, _session_key):
                return None

            async def get_sandbox_cwd(self, _session_key):
                return str(workspace)

        loop = AgentLoop(
            bus=MessageBus(),
            provider=provider,
            workspace=workspace,
            config=config,
            max_iterations=2,
            sandbox_manager=StubSandboxManager(workspace),
        )
        loop.tools.get_definitions = lambda: []
        loop.tools.execute = AsyncMock(
            side_effect=[
                (
                    "OpenViking search query: 授权登陆\n"
                    "Total matches: 2\n\n"
                    "Resources:\n"
                    "1. [document] viking://resources/xms-support/01-base.docx/01-base_2.md\n"
                    "   Content preview omitted. Use openviking_read for evidence.\n"
                    "2. [document] viking://resources/xms-support/01-base.docx/01-base_1.md\n"
                    "   Content preview omitted. Use openviking_read for evidence.\n"
                ),
                (
                    "3.1授权登录\n"
                    "在浏览器中输入系统的域名或 IP 地址，然后输入工号、密码，选择酒店和模块。"
                ),
                "目录中包含基础操作章节。",
            ]
        )

        response = asyncio.run(
            loop._process_message(
                InboundMessage(
                    sender_id="user-1",
                    content="如何进行授权登陆?",
                    session_key=SessionKey(type="dingtalk", channel_id="bot", chat_id="user-1"),
                )
            )
        )

    assert response is not None
    assert response.content == "授权登录时，先输入域名或IP，再填写工号、密码并选择酒店和模块。"
    assert loop.tools.execute.await_count == 3
    assert loop.tools.execute.await_args_list[0].args[0] == "openviking_search"
    assert loop.tools.execute.await_args_list[1].args[0] == "openviking_read"

    first_agent_messages = provider.calls[1]["messages"]
    assert any(message.get("role") == "tool" and "3.1授权登录" in message.get("content", "") for message in first_agent_messages)
    assert any(
        message.get("role") == "assistant"
        and any(tool_call["function"]["name"] == "openviking_search" for tool_call in message.get("tool_calls", []))
        for message in first_agent_messages
    )
