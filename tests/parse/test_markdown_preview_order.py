# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Tests for MarkdownParser preview-order manifest generation."""

import json
from typing import Any
from unittest.mock import patch

import pytest

from openviking.parse.parsers.markdown import MarkdownParser
from openviking_cli.utils.config.parser_config import ParserConfig


class FakeVikingFS:
    """Minimal VikingFS mock for MarkdownParser temp output."""

    def __init__(self):
        self.dirs: list[str] = []
        self.files: dict[str, bytes] = {}
        self._temp_counter = 0

    async def mkdir(self, uri: str, exist_ok: bool = False, **kwargs: Any) -> None:
        if uri not in self.dirs:
            self.dirs.append(uri)

    async def write_file(self, uri: str, content: Any) -> None:
        if isinstance(content, str):
            content = content.encode("utf-8")
        self.files[uri] = content

    def create_temp_uri(self) -> str:
        self._temp_counter += 1
        return f"viking://temp/md_{self._temp_counter}"


@pytest.mark.asyncio
async def test_markdown_parser_writes_preview_order_manifest() -> None:
    parser = MarkdownParser(config=ParserConfig(max_section_size=40, max_section_chars=10_000))
    parser.DEFAULT_MIN_SECTION_TOKENS = 1

    fake_fs = FakeVikingFS()
    content = """
# 一、总览

说明说明说明说明说明说明说明说明说明说明说明说明说明说明说明

# 二、设置

导语导语导语导语导语导语导语导语导语导语导语导语导语导语导语

## （一）准备

准备准备准备准备准备准备准备准备准备准备准备准备准备准备准备

## （二）执行

执行执行执行执行执行执行执行执行执行执行执行执行执行执行执行

# 十、附录

附录附录附录附录附录附录附录附录附录附录附录附录附录附录附录
""".strip()

    with patch.object(parser, "_get_viking_fs", return_value=fake_fs):
        result = await parser.parse_content(content, source_path="/tmp/资源库管理.md")

    root_dir = f"{result.temp_dir_path}/{parser._sanitize_for_path('资源库管理')}"
    manifest_uri = f"{root_dir}/{parser.PREVIEW_ORDER_FILENAME}"
    manifest = json.loads(fake_fs.files[manifest_uri].decode("utf-8"))

    assert manifest["markdown_paths"] == [
        "一总览.md",
        "二设置/二设置.md",
        "二设置/一准备.md",
        "二设置/二执行.md",
        "十附录.md",
    ]
