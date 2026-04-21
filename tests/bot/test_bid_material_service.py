# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for bid-material evidence service."""

import re
from pathlib import Path

import pytest

from vikingbot.services.bid_material import BidMaterialService


class FakeVikingClient:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.closed = False
        self.referenced_fragments: list[str] = []

    async def close(self) -> None:
        self.closed = True

    async def search(self, query: str, target_uri: str = "") -> dict:
        resources = [
            {
                "uri": "viking://resources/company/license/_images/page_1.png",
                "score": 0.98,
                "match_reason": "营业执照扫描件",
            },
            {
                "uri": "viking://resources/company/license/license.md",
                "score": 0.88,
                "match_reason": "营业执照说明",
            },
            {
                "uri": "viking://resources/solution/deploy.md",
                "score": 0.95,
                "match_reason": "部署方案说明",
            },
            {
                "uri": "viking://resources/solution/deploy/_images/diagram_1.png",
                "score": 0.80,
                "match_reason": "部署架构截图",
            },
            {
                "uri": "viking://resources/solution/.overview.md",
                "score": 0.70,
                "match_reason": "概览摘要",
            },
        ]
        return {"resources": resources, "memories": [], "skills": [], "total": len(resources)}

    async def resolve_read_uri(self, uri: str):
        normalized = uri.rstrip("/")
        if normalized.endswith("license.md"):
            return normalized, [normalized]
        if normalized.endswith("deploy.md"):
            return normalized, [normalized]
        if normalized.endswith("/license"):
            return f"{normalized}/license.md", [f"{normalized}/license.md"]
        return normalized, [normalized]

    async def read_content(self, uri: str, level: str = "read") -> str:
        if "license" in uri:
            return (
                "营业执照显示公司主体信息、统一社会信用代码和有效期。\n"
                "![营业执照扫描件](ov-asset://license.png)"
            )
        if "deploy" in uri:
            return (
                "部署方案支持本地化部署、双机容灾和分层架构。\n"
                "系统采用云原生微服务架构。\n"
                "![部署架构图](ov-asset://diagram_1.png)"
            )
        if "plain" in uri:
            return "纯文本方案说明，没有内联图片。"
        return ""

    async def export_uri_images(self, uri: str, output_dir: Path, max_images: int = 4):
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / "certificate.png"
        target.write_bytes(b"fake-image")
        return [
            {
                "source_uri": uri,
                "local_path": str(target),
                "caption": "营业执照",
                "page_hint": "1",
            }
        ][:max_images]

    async def export_related_images(self, uri: str, output_dir: Path, max_images: int = 4):
        output_dir.mkdir(parents=True, exist_ok=True)
        target = output_dir / "fallback-solution.png"
        target.write_bytes(b"fake-image")
        return [
            {
                "source_uri": f"{uri.rstrip('.md')}/_images/fallback_1.png",
                "local_path": str(target),
                "caption": "兜底配图",
                "page_hint": "",
            }
        ][:max_images]

    async def export_referenced_images(
        self, content: str, source_uri: str, output_dir: Path, max_images: int = 4
    ):
        self.referenced_fragments.append(content)
        output_dir.mkdir(parents=True, exist_ok=True)

        if "license.png" in content:
            target = output_dir / "certificate-inline.png"
            target.write_bytes(b"fake-inline-image")
            return [
                {
                    "source_uri": f"{source_uri.rstrip('.md')}/_images/license.png",
                    "local_path": str(target),
                    "caption": "营业执照扫描件",
                    "page_hint": "1",
                }
            ][:max_images]

        if "diagram_1.png" in content:
            target = output_dir / "solution-inline.png"
            target.write_bytes(b"fake-inline-image")
            return [
                {
                    "source_uri": f"{source_uri.rstrip('.md')}/_images/diagram_1.png",
                    "local_path": str(target),
                    "caption": "部署架构图",
                    "page_hint": "1",
                }
            ][:max_images]

        return []

    async def materialize_inline_image_refs_to_directory(
        self,
        content: str,
        source_uri: str,
        output_dir: Path,
    ) -> str:
        output_dir.mkdir(parents=True, exist_ok=True)

        replacements = {
            "ov-asset://license.png": output_dir / "certificate-inline.png",
            "ov-asset://diagram_1.png": output_dir / "solution-inline.png",
        }

        rendered = content
        for raw_ref, local_path in replacements.items():
            if raw_ref not in rendered:
                continue
            local_path.write_bytes(b"fake-inline-image")
            rendered = re.sub(
                rf"!\[([^\]]*)\]\({re.escape(raw_ref)}\)",
                lambda match: f"![{match.group(1)}]({local_path})",
                rendered,
            )

        return rendered


@pytest.mark.asyncio
async def test_search_certificates_returns_image_first_evidence(tmp_path: Path) -> None:
    async def factory(_context_id):
        return FakeVikingClient(tmp_path)

    service = BidMaterialService(client_factory=factory, artifact_root=tmp_path / "artifacts")

    pack = await service.search_certificates("营业执照")

    assert pack.intent == "certificate"
    assert pack.items
    assert pack.items[0].kind == "image"
    assert pack.items[0].images
    assert pack.items[0].images[0].local_path.endswith(".png")
    assert "营业执照扫描件" in pack.items[0].content_markdown
    assert ".png" in pack.items[0].content_markdown


@pytest.mark.asyncio
async def test_search_solution_materials_returns_excerpt_and_images(tmp_path: Path) -> None:
    async def factory(_context_id):
        return FakeVikingClient(tmp_path)

    service = BidMaterialService(client_factory=factory, artifact_root=tmp_path / "artifacts")

    pack = await service.search_solution_materials("部署方案")

    assert pack.intent == "solution"
    assert pack.items
    assert pack.items[0].excerpt
    assert "部署方案" in pack.items[0].excerpt or "部署" in pack.items[0].excerpt
    assert pack.items[0].images
    assert pack.items[0].images[0].caption == "部署架构图"
    assert pack.items[0].images[0].local_path.endswith("solution-inline.png")
    assert "云原生微服务架构" in pack.items[0].content_markdown
    assert "部署架构图" in pack.items[0].content_markdown
    assert "ov-asset://" not in pack.items[0].content_markdown


@pytest.mark.asyncio
async def test_search_solution_materials_falls_back_to_related_images_when_no_inline_anchor(tmp_path: Path) -> None:
    class PlainTextClient(FakeVikingClient):
        async def search(self, query: str, target_uri: str = "") -> dict:
            return {
                "resources": [
                    {
                        "uri": "viking://resources/solution/plain.md",
                        "score": 0.91,
                        "match_reason": "纯文本方案",
                    }
                ],
                "memories": [],
                "skills": [],
                "total": 1,
            }

        async def resolve_read_uri(self, uri: str):
            normalized = uri.rstrip("/")
            return normalized, [normalized]

    async def factory(_context_id):
        return PlainTextClient(tmp_path)

    service = BidMaterialService(client_factory=factory, artifact_root=tmp_path / "artifacts")

    pack = await service.search_solution_materials("纯文本方案")

    assert pack.items
    assert pack.items[0].images
    assert pack.items[0].images[0].caption == "兜底配图"
    assert pack.items[0].images[0].local_path.endswith("fallback-solution.png")
    assert "纯文本方案说明" in pack.items[0].content_markdown
    assert "fallback-solution.png" in pack.items[0].content_markdown


@pytest.mark.asyncio
async def test_collect_bid_evidence_returns_claim_coverage(tmp_path: Path) -> None:
    async def factory(_context_id):
        return FakeVikingClient(tmp_path)

    service = BidMaterialService(client_factory=factory, artifact_root=tmp_path / "artifacts")

    pack = await service.collect_bid_evidence(
        section_name="商务资信",
        requirement="需要提供营业执照；需要说明部署方案。",
    )

    assert pack.intent == "section_evidence"
    assert pack.claims
    assert {claim.status for claim in pack.claims}.issubset({"supported", "partial", "missing"})
    assert pack.items


@pytest.mark.asyncio
async def test_pack_to_dict_exposes_minimal_markdown_first_payload(tmp_path: Path) -> None:
    async def factory(_context_id):
        return FakeVikingClient(tmp_path)

    service = BidMaterialService(client_factory=factory, artifact_root=tmp_path / "artifacts")

    pack = await service.search_solution_materials("部署方案")
    payload = service.pack_to_dict(pack)

    assert sorted(payload.keys()) == ["intent", "items", "query", "summary", "target_uri"]
    assert sorted(payload["items"][0].keys()) == ["content_markdown", "id", "source_uri", "title"]
    assert "部署架构图" in payload["items"][0]["content_markdown"]
