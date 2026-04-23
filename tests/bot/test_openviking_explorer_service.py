"""Tests for low-level OpenViking MCP exploration helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from vikingbot.services.openviking_explorer import OpenVikingExplorerService


class FakeExplorerClient:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True

    async def read_content(self, uri: str, level: str = "abstract") -> str:
        if uri.endswith(".abstract.md"):
            return "摘要内容"
        return "正文内容\n\n![图示](ov-asset://image17.png)"

    async def resolve_read_uri(self, uri: str):
        if uri.endswith("/folder"):
            return None, ["viking://resources/demo/folder/doc.md"]
        if uri.endswith("/summary-parent"):
            return "viking://resources/demo/summary-parent/doc.md", [
                "viking://resources/demo/summary-parent/doc.md"
            ]
        return uri, []

    async def stat(self, uri: str):
        if uri.endswith(".png"):
            return {"isDir": False}
        if uri.endswith("/folder"):
            return {"isDir": True}
        if uri.endswith(".abstract.md"):
            return {"isDir": False}
        return {"isDir": False}

    async def export_uri_images(self, uri: str, output_dir: Path, max_images: int | None = 8):
        return [{"local_path": str(output_dir / "img.png"), "caption": "img", "source_uri": uri}]

    async def materialize_inline_image_refs_to_directory(
        self, content: str, source_uri: str, output_dir: Path
    ) -> str:
        return content.replace("ov-asset://image17.png", str(output_dir / "image17.png"))

    async def export_related_images(self, uri: str, output_dir: Path, max_images: int | None = 8):
        return [{"local_path": str(output_dir / "related.png"), "caption": "related", "source_uri": uri}]

    async def list_resources(self, path: str | None = None, recursive: bool = False):
        return [
            {"name": "folder", "uri": "viking://resources/demo/folder", "isDir": True, "size": 0},
            {"name": "doc.md", "uri": "viking://resources/demo/doc.md", "isDir": False, "size": 12},
        ]

    async def glob(self, pattern: str, uri: str | None = None):
        return {"count": 1, "matches": ["viking://resources/demo/doc.md"]}

    async def search(self, query: str, target_uri: str = ""):
        return {
            "resources": [
                {
                    "uri": "viking://resources/demo/doc.md",
                    "score": 0.8,
                    "match_reason": "doc match",
                    "abstract": "文档摘要",
                    "category": "",
                    "context_type": "",
                },
                {
                    "uri": "viking://resources/demo/.abstract.md",
                    "score": 0.7,
                    "match_reason": "summary match",
                    "abstract": "摘要",
                    "category": "",
                    "context_type": "",
                },
                {
                    "uri": "viking://resources/demo/_images/image1.png",
                    "score": 0.6,
                    "match_reason": "image match",
                    "abstract": "",
                    "category": "",
                    "context_type": "",
                },
            ],
            "memories": [],
            "skills": [],
            "total": 3,
            "query": query,
            "target_uri": target_uri,
        }


async def _fake_client_factory(agent_id: str | None = None) -> FakeExplorerClient:
    return FakeExplorerClient()


@pytest.mark.asyncio
async def test_openviking_read_returns_summary_guidance() -> None:
    service = OpenVikingExplorerService(
        client_factory=_fake_client_factory,
        artifact_root=Path("/tmp/openviking-explorer-test-summary"),
    )

    payload = await service.openviking_read(
        uri="viking://resources/demo/summary-parent/.abstract.md",
        level="read",
    )

    assert payload["kind"] == "summary"
    assert payload["resolved_uri"] == "viking://resources/demo/summary-parent/doc.md"
    assert payload["candidate_uris"] == ["viking://resources/demo/summary-parent/doc.md"]
    assert "导航" in payload["note"] or "navigation" in payload["note"].lower()


@pytest.mark.asyncio
async def test_openviking_read_materializes_local_images(tmp_path: Path) -> None:
    service = OpenVikingExplorerService(
        client_factory=_fake_client_factory,
        artifact_root=tmp_path,
    )

    payload = await service.openviking_read(
        uri="viking://resources/demo/doc.md",
        level="read",
    )

    assert payload["kind"] == "document"
    assert payload["image_count"] == 1
    assert "image17.png" in payload["content_markdown"]
    assert "ov-asset://" not in payload["content_markdown"]


@pytest.mark.asyncio
async def test_openviking_search_groups_documents_images_and_summaries() -> None:
    service = OpenVikingExplorerService(
        client_factory=_fake_client_factory,
        artifact_root=Path("/tmp/openviking-explorer-test-search"),
    )

    payload = await service.openviking_search(query="架构图", target_uri="viking://resources/demo")

    assert payload["total"] == 3
    assert payload["documents"][0]["uri"] == "viking://resources/demo/doc.md"
    assert payload["images"][0]["kind"] == "image"
    assert payload["summaries"][0]["kind"] == "summary"
    assert "openviking_read" in payload["next_step"]
