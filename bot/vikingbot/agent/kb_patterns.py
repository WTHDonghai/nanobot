"""Shared regex patterns for KB evidence and response helpers."""

from __future__ import annotations

import re

SEND_IMAGE_LINE_RE = re.compile(r"!\[[^\]]*\]\((send://[^)\s]+)\)")
MARKDOWN_IMAGE_LINE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+.+\s*$")
NUMBERED_HEADING_RE = re.compile(
    r"^\s*(?:\*\*)?\s*(?:第[一二三四五六七八九十百千]+[章节节、]|"
    r"[一二三四五六七八九十]+[、.．]|"
    r"\d+(?:\.\d+){0,4})\s*[\u4e00-\u9fffA-Za-z][^\n]{0,80}?(?:\*\*)?\s*$"
)
