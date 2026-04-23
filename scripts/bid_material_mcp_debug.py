#!/usr/bin/env python3
"""Debug helper for the bid-material MCP server."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO


DEFAULT_PROTOCOL = "line"
DEFAULT_TARGET_URI = "viking://resources/"
DEFAULT_OUTPUT = "parsed"


class MCPDebugError(RuntimeError):
    """Raised when the debug client cannot talk to the MCP server."""


def _server_command(python_bin: str, config_path: str) -> list[str]:
    return [python_bin, "-m", "vikingbot", "bid-material-mcp", "-c", config_path]


def _send_line(stream: BinaryIO, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stream.write(body)
    stream.write(b"\n")
    stream.flush()


def _recv_line(stream: BinaryIO) -> dict[str, Any]:
    line = stream.readline()
    if not line:
        raise MCPDebugError("MCP server closed stdout before replying.")
    return json.loads(line.decode("utf-8"))


def _send_framed(stream: BinaryIO, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    stream.write(header)
    stream.write(body)
    stream.flush()


def _recv_framed(stream: BinaryIO) -> dict[str, Any]:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            raise MCPDebugError("MCP server closed stdout before replying.")
        if line in {b"\r\n", b"\n"}:
            break
        key, value = line.decode("utf-8").split(":", 1)
        headers[key.strip().lower()] = value.strip()

    content_length = int(headers.get("content-length", "0"))
    if content_length <= 0:
        raise MCPDebugError(f"Invalid Content-Length from MCP server: {headers!r}")
    body = stream.read(content_length)
    if not body:
        raise MCPDebugError("MCP server returned an empty framed body.")
    return json.loads(body.decode("utf-8"))


def _send(stream: BinaryIO, payload: dict[str, Any], protocol: str) -> None:
    if protocol == "framed":
        _send_framed(stream, payload)
        return
    _send_line(stream, payload)


def _recv(stream: BinaryIO, protocol: str) -> dict[str, Any]:
    if protocol == "framed":
        return _recv_framed(stream)
    return _recv_line(stream)


def _initialize(proc: subprocess.Popen[bytes], protocol: str) -> list[dict[str, Any]]:
    assert proc.stdin is not None
    assert proc.stdout is not None

    initialize_request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    _send(proc.stdin, initialize_request, protocol)
    initialize_response = _recv(proc.stdout, protocol)

    initialized_notification = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
    _send(proc.stdin, initialized_notification, protocol)

    list_tools_request = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    _send(proc.stdin, list_tools_request, protocol)
    list_tools_response = _recv(proc.stdout, protocol)
    return [initialize_response, list_tools_response]


def _call_tool(
    proc: subprocess.Popen[bytes],
    *,
    protocol: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    assert proc.stdin is not None
    assert proc.stdout is not None
    request = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    _send(proc.stdin, request, protocol)
    return _recv(proc.stdout, protocol)


def _extract_text_result(response: dict[str, Any]) -> Any:
    result = response.get("result") or {}
    content = result.get("content") or []
    if not content:
        return response
    text = content[0].get("text", "")
    if not text:
        return response
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Debug the bid-material MCP server over stdio.")
    parser.add_argument(
        "--python",
        default=str(Path.cwd() / ".venv" / "bin" / "python"),
        help="Python executable used to launch `-m vikingbot bid-material-mcp`.",
    )
    parser.add_argument(
        "--config",
        default=str(Path.home() / ".openviking" / "ov-bidding.conf"),
        help="OpenViking config path passed to `bid-material-mcp`.",
    )
    parser.add_argument(
        "--protocol",
        choices=("line", "framed"),
        default=DEFAULT_PROTOCOL,
        help="Stdio wire format to use for debugging.",
    )
    parser.add_argument(
        "--output",
        choices=("parsed", "full"),
        default=DEFAULT_OUTPUT,
        help="Print only the parsed tool payload, or the full MCP debug envelope.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list-tools", help="Initialize the server and print the exposed tools.")

    call_parser = subparsers.add_parser("call", help="Call one MCP tool with raw JSON arguments.")
    call_parser.add_argument("--tool", required=True, help="Tool name, e.g. search_certificates.")
    call_parser.add_argument(
        "--arguments-json",
        default="{}",
        help='Tool arguments as JSON, e.g. \'{"query":"ISO"}\'.',
    )

    cert_parser = subparsers.add_parser("cert", help="Call search_certificates.")
    cert_parser.add_argument("--query", required=True)
    cert_parser.add_argument("--target-uri", default=DEFAULT_TARGET_URI)
    cert_parser.add_argument("--top-k", type=int, default=5)

    solution_parser = subparsers.add_parser("solution", help="Call search_solution_materials.")
    solution_parser.add_argument("--query", required=True)
    solution_parser.add_argument("--target-uri", default=DEFAULT_TARGET_URI)
    solution_parser.add_argument("--top-k", type=int, default=5)

    evidence_parser = subparsers.add_parser("evidence", help="Call collect_bid_evidence.")
    evidence_parser.add_argument("--section-name", required=True)
    evidence_parser.add_argument("--requirement", required=True)
    evidence_parser.add_argument("--target-uri", default=DEFAULT_TARGET_URI)
    evidence_parser.add_argument("--top-k", type=int, default=8)

    ov_search_parser = subparsers.add_parser("ov-search", help="Call openviking_search.")
    ov_search_parser.add_argument("--query", required=True)
    ov_search_parser.add_argument("--target-uri", default=DEFAULT_TARGET_URI)

    ov_list_parser = subparsers.add_parser("ov-list", help="Call openviking_list.")
    ov_list_parser.add_argument("--uri", default=DEFAULT_TARGET_URI)
    ov_list_parser.add_argument("--recursive", action="store_true")

    ov_glob_parser = subparsers.add_parser("ov-glob", help="Call openviking_glob.")
    ov_glob_parser.add_argument("--pattern", required=True)
    ov_glob_parser.add_argument("--uri", default=DEFAULT_TARGET_URI)

    ov_read_parser = subparsers.add_parser("ov-read", help="Call openviking_read.")
    ov_read_parser.add_argument("--uri", required=True)
    ov_read_parser.add_argument("--level", choices=("abstract", "overview", "read"), default="read")
    ov_read_parser.add_argument("--include-images", dest="include_images", action="store_true", default=True)
    ov_read_parser.add_argument("--no-include-images", dest="include_images", action="store_false")
    ov_read_parser.add_argument("--max-images", type=int, default=8)
    return parser


def _command_arguments(args: argparse.Namespace) -> tuple[str | None, dict[str, Any] | None]:
    if args.command == "call":
        return args.tool, json.loads(args.arguments_json)
    if args.command == "cert":
        return "search_certificates", {
            "query": args.query,
            "target_uri": args.target_uri,
            "top_k": args.top_k,
        }
    if args.command == "solution":
        return "search_solution_materials", {
            "query": args.query,
            "target_uri": args.target_uri,
            "top_k": args.top_k,
        }
    if args.command == "evidence":
        return "collect_bid_evidence", {
            "section_name": args.section_name,
            "requirement": args.requirement,
            "target_uri": args.target_uri,
            "top_k": args.top_k,
        }
    if args.command == "ov-search":
        return "openviking_search", {
            "query": args.query,
            "target_uri": args.target_uri,
        }
    if args.command == "ov-list":
        return "openviking_list", {
            "uri": args.uri,
            "recursive": args.recursive,
        }
    if args.command == "ov-glob":
        return "openviking_glob", {
            "pattern": args.pattern,
            "uri": args.uri,
        }
    if args.command == "ov-read":
        return "openviking_read", {
            "uri": args.uri,
            "level": args.level,
            "include_images": args.include_images,
            "max_images": args.max_images,
        }
    return None, None


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    command = _server_command(args.python, args.config)

    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        init_response, list_tools_response = _initialize(proc, args.protocol)
        payload: dict[str, Any] = {
            "command": args.command,
            "protocol": args.protocol,
            "server": command,
            "initialize": init_response,
            "tools": list_tools_response.get("result", {}).get("tools", []),
        }

        tool_name, tool_arguments = _command_arguments(args)
        if tool_name is not None and tool_arguments is not None:
            call_response = _call_tool(
                proc,
                protocol=args.protocol,
                tool_name=tool_name,
                arguments=tool_arguments,
            )
            parsed_result = _extract_text_result(call_response)
            payload["tool_call"] = {
                "name": tool_name,
                "arguments": tool_arguments,
                "response": call_response,
                "parsed_result": parsed_result,
            }

        if args.output == "full":
            output: Any = payload
        elif args.command == "list-tools":
            output = payload["tools"]
        else:
            output = payload.get("tool_call", {}).get("parsed_result", payload)

        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        stderr = b""
        if proc.stderr is not None:
            try:
                stderr = proc.stderr.read()
            except Exception:
                stderr = b""
        if stderr:
            sys.stderr.write(stderr.decode("utf-8", "replace"))
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    raise SystemExit(main())
