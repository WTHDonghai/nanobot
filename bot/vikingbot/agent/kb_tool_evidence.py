"""Tool-result evidence helpers for KB retrieval."""

from __future__ import annotations

import json
from typing import Any

from vikingbot.openviking_mount.uri_utils import is_generic_scope_summary_uri, is_summary_uri


class KbToolEvidenceMixin:
    """Helpers for classifying stored tool records as concrete KB evidence."""

    @classmethod
    def _concrete_read_uris(cls, tools_used: list[dict[str, Any]]) -> list[str]:
        """Return concrete openviking_read URIs from tool records."""
        uris: list[str] = []
        seen: set[str] = set()
        for tool in tools_used:
            if not cls._is_concrete_read_tool_record(tool):
                continue
            args = cls._parse_tool_args(tool.get("args"))
            uri = str(args.get("uri") or "").strip()
            if uri and uri not in seen:
                seen.add(uri)
                uris.append(uri)
        return uris

    @staticmethod
    def _parse_tool_args(raw_args: Any) -> dict[str, Any]:
        """Parse tool args stored as JSON text in tests/session history."""
        if isinstance(raw_args, dict):
            return raw_args
        if not isinstance(raw_args, str) or not raw_args.strip():
            return {}
        try:
            parsed = json.loads(raw_args)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @classmethod
    def _is_concrete_read_tool_record(cls, tool_record: dict[str, Any]) -> bool:
        """Whether a stored tool record is a successful concrete openviking_read."""
        if tool_record.get("tool_name") != "openviking_read":
            return False
        result = tool_record.get("result")
        if (
            not isinstance(result, str)
            or not result.strip()
            or cls._classify_tool_error_result(result)
        ):
            return False
        args = cls._parse_tool_args(tool_record.get("args"))
        uri = str(args.get("uri") or "").strip()
        level = str(args.get("level", "abstract") or "abstract")
        return bool(
            uri
            and level == "read"
            and not is_summary_uri(uri)
            and not is_generic_scope_summary_uri(uri)
        )
