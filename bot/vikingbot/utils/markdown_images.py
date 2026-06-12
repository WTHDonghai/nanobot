"""Helpers for preserving bot-sendable Markdown image references."""

from __future__ import annotations

import re
from collections.abc import Callable

SEND_IMAGE_MARKDOWN_RE = re.compile(r"!\[([^\]]*)\]\((send://[^)\s]+)\)")
BROKEN_SEND_IMAGE_TAIL_RE = re.compile(
    r"(^|[\s，。；;:：])\]\((send://[^)\s]+)\)([A-Za-z0-9_\-.]{1,80})",
    re.MULTILINE,
)
BARE_SEND_IMAGE_RE = re.compile(r"(?<!\]\()(?<!\]\(send://)(send://[^\s)>\"']+)")


def repair_send_image_markdown(content: str | None) -> str | None:
    """Repair common model-damaged send:// image Markdown without touching valid images."""
    if not isinstance(content, str) or "send://" not in content:
        return content

    def repair_segment(segment: str) -> str:
        def replace_broken_tail(match: re.Match[str]) -> str:
            prefix = match.group(1)
            ref = match.group(2)
            alt = match.group(3).strip() or _alt_from_send_ref(ref)
            return f"{prefix}![{alt}]({ref})"

        repaired = BROKEN_SEND_IMAGE_TAIL_RE.sub(replace_broken_tail, segment)

        def replace_bare(match: re.Match[str]) -> str:
            ref = match.group(1)
            return f"![{_alt_from_send_ref(ref)}]({ref})"

        return BARE_SEND_IMAGE_RE.sub(replace_bare, repaired)

    return _map_non_fenced_segments(content, repair_segment)


def strip_send_image_markdown(content: str | None) -> str | None:
    """Remove send:// image Markdown and common damaged fragments from model-authored prose."""
    if not isinstance(content, str) or "send://" not in content:
        return content

    repaired = repair_send_image_markdown(content) or ""

    def strip_segment(segment: str) -> str:
        stripped = SEND_IMAGE_MARKDOWN_RE.sub("", segment)
        stripped = re.sub(r"(?m)^\s*!\[\s*$", "", stripped)
        stripped = re.sub(r"(?m)^\s*\]\(send://[^\s)]+\)\S*\s*$", "", stripped)
        return stripped

    stripped = _map_non_fenced_segments(repaired, strip_segment)
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)
    return stripped.strip()


def _alt_from_send_ref(ref: str) -> str:
    filename = str(ref or "").rsplit("/", 1)[-1]
    if filename.startswith("send://"):
        filename = filename[len("send://") :]
    stem = filename.rsplit(".", 1)[0].strip()
    return stem or "image"


def _map_non_fenced_segments(content: str, transform: Callable[[str], str]) -> str:
    segments = re.split(r"(```[\s\S]*?```)", content)
    return "".join(segment if index % 2 else transform(segment) for index, segment in enumerate(segments))
