#!/usr/bin/env python3
"""Validate the OpenViking design-document set without changing files."""

from __future__ import annotations

import re
import sys
from pathlib import Path


DOC_DIR = Path("docs/design/admin-session-performance")
ROADMAP = Path("docs/design/production-readiness-roadmap.md")
REQUIRED_DOCS = (
    "README.md",
    "01-analysis.md",
    "02-requirements.md",
    "03-design.md",
    "04-test-plan.md",
    "05-implementation-plan.md",
    "06-migration-runbook.md",
)
ALLOWED_STATUSES = {
    "Draft",
    "In Review",
    "Approved",
    "Implementing",
    "Validated",
    "Active",
    "Completed",
    "Superseded",
    "Archived",
}
OLD_FILENAMES = (
    "admin-session-performance-analysis.md",
    "admin-session-performance-requirements.md",
    "admin-session-performance-design.md",
    "admin-session-performance-test-plan.md",
    "admin-session-performance-implementation-plan.md",
    "admin-session-performance-migration-runbook.md",
)
LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
STATUS_RE = re.compile(r"^状态：(.+?)\s*$", re.MULTILINE)


def add_error(errors: list[str], path: Path, message: str) -> None:
    errors.append(f"{path}: {message}")


def validate_file(path: Path, errors: list[str]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        add_error(errors, path, f"cannot read: {exc}")
        return

    if not text.endswith("\n"):
        add_error(errors, path, "missing final newline")

    for number, line in enumerate(text.splitlines(), start=1):
        if line.endswith((" ", "\t")):
            add_error(errors, path, f"line {number}: trailing whitespace")

    if sum(1 for line in text.splitlines() if line.startswith("```")) % 2:
        add_error(errors, path, "unbalanced fenced code blocks")

    status_match = STATUS_RE.search(text)
    if not status_match:
        add_error(errors, path, "missing 状态 field")
    else:
        raw_status = status_match.group(1).split("，", 1)[0].strip()
        if raw_status not in ALLOWED_STATUSES:
            add_error(errors, path, f"unsupported status: {raw_status}")

    for old_name in OLD_FILENAMES:
        if old_name in text:
            add_error(errors, path, f"references old filename: {old_name}")

    for raw_target in LINK_RE.findall(text):
        target_text = raw_target.split("#", 1)[0]
        if not target_text or re.match(r"^(?:https?:|mailto:)", target_text):
            continue
        target = (path.parent / target_text).resolve()
        if not target.exists():
            add_error(errors, path, f"broken local link: {raw_target}")


def main() -> int:
    errors: list[str] = []
    paths = [DOC_DIR / name for name in REQUIRED_DOCS] + [ROADMAP]

    for path in paths:
        if not path.is_file():
            add_error(errors, path, "required document is missing")

    for path in paths:
        if path.is_file():
            validate_file(path, errors)

    if errors:
        print("design-doc validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"design-doc validation passed: {len(paths)} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
