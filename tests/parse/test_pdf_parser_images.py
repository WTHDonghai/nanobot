# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for PDFParser extracted image persistence."""

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from openviking.parse.base import NodeType, ResourceNode, create_parse_result
from openviking.parse.parsers.pdf import PDFParser

PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z0WQAAAAASUVORK5CYII="
)


class FakeVikingFS:
    """Minimal VikingFS mock for parser output assertions."""

    def __init__(self):
        self.dirs: list[str] = []
        self.files: dict[str, bytes] = {}
        self._temp_counter = 0

    async def mkdir(self, uri: str, exist_ok: bool = False, **kwargs: Any) -> None:
        if uri not in self.dirs:
            self.dirs.append(uri)

    async def write(self, uri: str, data: Any) -> str:
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.files[uri] = data
        return uri

    async def ls(self, uri: str) -> list[dict[str, Any]]:
        prefix = uri.rstrip("/") + "/"
        children: dict[str, bool] = {}

        for key in list(self.files.keys()) + self.dirs:
            if not key.startswith(prefix):
                continue
            rest = key[len(prefix) :]
            if not rest:
                continue
            name = rest.split("/")[0]
            child_uri = f"{prefix}{name}"
            is_dir = "/" in rest[len(name) :] or child_uri in self.dirs
            children[name] = children.get(name, False) or is_dir

        return [
            {"name": name, "uri": f"{uri.rstrip('/')}/{name}", "isDir": is_dir}
            for name, is_dir in sorted(children.items())
        ]

    def create_temp_uri(self) -> str:
        self._temp_counter += 1
        return f"viking://temp/pdf_{self._temp_counter}"


class FakePDFStream:
    """Minimal PDF stream double exposing get_data()."""

    def __init__(self, data: bytes):
        self._data = data

    def get_data(self) -> bytes:
        return self._data


@pytest.mark.asyncio
async def test_pdf_parser_persists_extracted_images_under_document_root(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "proposal.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake pdf")

    fake_fs = FakeVikingFS()
    temp_uri = fake_fs.create_temp_uri()
    doc_dir = f"{temp_uri}/proposal"
    await fake_fs.mkdir(temp_uri)
    await fake_fs.mkdir(doc_dir)

    parse_result = create_parse_result(
        root=ResourceNode(type=NodeType.ROOT, title="Proposal", level=0),
        source_path=str(pdf_path),
        source_format="markdown",
        parser_name="MarkdownParser",
        parse_time=0.1,
    )
    parse_result.temp_dir_path = temp_uri

    parser = PDFParser()
    parser._convert_to_markdown = AsyncMock(
        return_value=(
            "# 投标方案\n\n![Page 1 Image 1](ov-asset://page1_img1.png)",
            {"images_extracted": 1},
            [("page1_img1.png", PNG_BYTES)],
        )
    )
    parser._markdown_parser = SimpleNamespace(parse_content=AsyncMock(return_value=parse_result))

    monkeypatch.setattr("openviking.storage.viking_fs.get_viking_fs", lambda: fake_fs)

    result = await parser.parse(pdf_path)

    image_uris = [uri for uri in fake_fs.files if uri.startswith(f"{doc_dir}/_images/")]
    assert image_uris == [f"{doc_dir}/_images/page1_img1.png"]
    assert fake_fs.files[image_uris[0]] == PNG_BYTES
    assert result.meta["embedded_image_count"] == 1
    assert result.meta["embedded_images_dir"] == "_images"

    markdown_content = parser._markdown_parser.parse_content.await_args.args[0]
    assert "ov-asset://page1_img1.png" in markdown_content


def test_pdf_parser_extracts_image_bytes_from_img_info_stream() -> None:
    parser = PDFParser()
    image_bytes = parser._extract_image_from_page(
        page=SimpleNamespace(),
        img_info={"stream": FakePDFStream(PNG_BYTES)},
    )

    assert image_bytes == PNG_BYTES
