# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
PDF parser for OpenViking.

Unified parser that converts PDF to Markdown then parses the result.
Supports dual strategy:
- Local: pdfplumber for direct conversion
- Remote: MinerU API for advanced conversion

This design simplifies PDF handling by delegating structure analysis
to the MarkdownParser after conversion.
"""

import io
import logging
import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from PIL import Image

from openviking.parse.base import (
    NodeType,
    ParseResult,
    ResourceNode,
    create_parse_result,
    lazy_import,
)
from openviking.parse.parsers.base_parser import BaseParser
from openviking_cli.utils.config.parser_config import PDFConfig

logger = logging.getLogger(__name__)


class PDFParser(BaseParser):
    EMBEDDED_IMAGES_DIR = "_images"
    IMAGE_PLACEHOLDER_SCHEME = "ov-asset://"

    """
    PDF parser with dual conversion strategy.

    Converts PDF → Markdown → ParseResult using MarkdownParser.
    When available, extracts PDF bookmarks/outlines and injects them as
    markdown headings so MarkdownParser can build a hierarchical directory
    structure instead of flat numbered files.

    Strategies:
    - "local": Use pdfplumber for text and table extraction
    - "mineru": Use MinerU API for advanced PDF processing
    - "auto": Try local first, fallback to MinerU if configured

    Examples:
        >>> # Local parsing
        >>> parser = PDFParser(PDFConfig(strategy="local"))
        >>> result = await parser.parse("document.pdf")

        >>> # Remote API parsing
        >>> config = PDFConfig(
        ...     strategy="mineru",
        ...     mineru_endpoint="https://api.example.com/convert",
        ...     mineru_api_key="key"
        ... )
        >>> parser = PDFParser(config)
        >>> result = await parser.parse("document.pdf")
    """

    def __init__(self, config: Optional[PDFConfig] = None):
        """
        Initialize PDF parser.

        Args:
            config: PDFConfig instance (defaults to auto strategy)
        """
        self.config = config or PDFConfig()
        self.config.validate()

        # Lazy import MarkdownParser to avoid circular imports
        self._markdown_parser = None

    def _get_markdown_parser(self):
        """Lazy import and create MarkdownParser."""
        if self._markdown_parser is None:
            from openviking.parse.parsers.markdown import MarkdownParser

            self._markdown_parser = MarkdownParser()
        return self._markdown_parser

    @property
    def supported_extensions(self) -> List[str]:
        """List of supported file extensions."""
        return [".pdf"]

    async def parse(self, source: Union[str, Path], instruction: str = "", **kwargs) -> ParseResult:
        """
        Parse PDF file.

        Args:
            source: Path to PDF file
            **kwargs: Additional options (currently unused)

        Returns:
            ParseResult with document tree

        Raises:
            FileNotFoundError: If PDF file doesn't exist
            ValueError: If conversion fails with all strategies
        """
        start_time = time.time()
        pdf_path = Path(source)

        if not pdf_path.exists():
            return create_parse_result(
                root=ResourceNode(type=NodeType.ROOT),
                source_path=str(pdf_path),
                source_format="pdf",
                parser_name="PDFParser",
                parse_time=time.time() - start_time,
                warnings=[f"File not found: {pdf_path}"],
            )

        try:
            # Step 1: Convert PDF to Markdown
            markdown_content, conversion_meta, embedded_images = await self._convert_to_markdown(
                pdf_path
            )

            # Step 2: Parse Markdown using MarkdownParser
            md_parser = self._get_markdown_parser()
            result = await md_parser.parse_content(
                markdown_content,
                source_path=str(pdf_path),
                instruction=instruction,
                **kwargs,
            )

            written_images = await self._write_embedded_images(
                result.temp_dir_path, embedded_images
            )

            # Step 3: Update metadata for PDF origin
            result.source_format = "pdf"  # Override markdown format
            result.parser_name = "PDFParser"
            result.parser_version = "2.0"
            result.parse_time = time.time() - start_time
            result.meta.update(conversion_meta)
            result.meta["embedded_image_count"] = len(written_images)
            if written_images:
                result.meta["embedded_images_dir"] = self.EMBEDDED_IMAGES_DIR
            result.meta["pdf_strategy"] = self.config.strategy
            result.meta["intermediate_markdown_length"] = len(markdown_content)
            result.meta["intermediate_markdown_preview"] = markdown_content[:500]

            logger.info(
                f"PDF parsed successfully: {pdf_path.name} "
                f"({len(markdown_content)} chars markdown, "
                f"{result.parse_time:.2f}s)"
            )

            return result

        except Exception as e:
            logger.error(f"Failed to parse PDF {pdf_path}: {e}")
            return create_parse_result(
                root=ResourceNode(type=NodeType.ROOT),
                source_path=str(pdf_path),
                source_format="pdf",
                parser_name="PDFParser",
                parse_time=time.time() - start_time,
                warnings=[f"Failed to parse PDF: {e}"],
            )

    async def _convert_to_markdown(
        self, pdf_path: Path
    ) -> tuple[str, Dict[str, Any], list[tuple[str, bytes]]]:
        """
        Convert PDF to Markdown using configured strategy.

        Args:
            pdf_path: Path to PDF file

        Returns:
            Tuple of (markdown_content, metadata_dict)

        Raises:
            ValueError: If all conversion strategies fail
        """
        if self.config.strategy == "local":
            return await self._convert_local(pdf_path)

        elif self.config.strategy == "mineru":
            return await self._convert_mineru(pdf_path)

        elif self.config.strategy == "auto":
            # Try local first
            try:
                return await self._convert_local(pdf_path)
            except Exception as e:
                logger.warning(f"Local conversion failed: {e}")

                # Fallback to MinerU if configured
                if self.config.mineru_endpoint:
                    logger.info("Falling back to MinerU API")
                    return await self._convert_mineru(pdf_path)
                else:
                    raise ValueError(
                        f"Local conversion failed and no MinerU endpoint configured: {e}"
                    )

        else:
            raise ValueError(f"Unknown strategy: {self.config.strategy}")

    async def _convert_local(
        self, pdf_path: Path
    ) -> tuple[str, Dict[str, Any], list[tuple[str, bytes]]]:
        """
        Convert PDF to Markdown using pdfplumber.

        When the PDF contains bookmarks/outlines, these are extracted and
        injected as markdown headings at the appropriate page positions.
        This allows MarkdownParser to build a hierarchical directory tree
        instead of producing flat numbered files.

        Args:
            pdf_path: Path to PDF file
        Returns:
            Tuple of (markdown_content, metadata, extracted_images)

        Raises:
            ImportError: If pdfplumber not installed
            Exception: If conversion fails
        """
        pdfplumber = lazy_import("pdfplumber")

        parts = []
        embedded_images: list[tuple[str, bytes]] = []
        meta = {
            "strategy": "local",
            "library": "pdfplumber",
            "pages_processed": 0,
            "images_extracted": 0,
            "tables_extracted": 0,
            "bookmarks_found": 0,
            "heading_source": "none",
        }

        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                meta["total_pages"] = len(pdf.pages)

                # Extract structure (bookmarks → font fallback)
                detection_mode = self.config.heading_detection
                bookmarks = []
                heading_source = "none"

                if detection_mode in ("bookmarks", "auto"):
                    bookmarks = self._extract_bookmarks(pdf)
                    if bookmarks:
                        heading_source = "bookmarks"

                if not bookmarks and detection_mode in ("font", "auto"):
                    bookmarks = self._detect_headings_by_font(pdf)
                    if bookmarks:
                        heading_source = "font_analysis"

                meta["bookmarks_found"] = len(bookmarks)
                meta["heading_source"] = heading_source
                logger.info(f"Heading detection: {heading_source}, found {len(bookmarks)} headings")

                # Group bookmarks by page_num
                bookmarks_by_page = defaultdict(list)
                for bm in bookmarks:
                    # Fall back to page 1 for unresolvable destinations
                    page = bm["page_num"] or 1
                    bookmarks_by_page[page].append(bm)

                for page_num, page in enumerate(pdf.pages, 1):
                    # Inject headings before page text
                    page_bookmarks = bookmarks_by_page.get(page_num, [])
                    for bm in page_bookmarks:
                        heading_prefix = "#" * bm["level"]
                        parts.append(f"\n{heading_prefix} {bm['title']}\n")

                    # Extract text
                    text = page.extract_text()
                    if text and text.strip():
                        # Add page marker as HTML comment
                        parts.append(f"<!-- Page {page_num} -->\n{text.strip()}")
                        meta["pages_processed"] += 1

                    # Extract tables
                    tables = page.extract_tables()
                    for table_idx, table in enumerate(tables or []):
                        if table and len(table) > 0:
                            md_table = self._format_table_markdown(table)
                            if md_table:
                                parts.append(
                                    f"<!-- Page {page_num} Table {table_idx + 1} -->\n{md_table}"
                                )
                                meta["tables_extracted"] += 1

                    # Extract images
                    images = page.images
                    for img_idx, img in enumerate(images or []):
                        try:
                            # Extract image using underlying PDF object
                            image_obj = self._extract_image_from_page(page, img)
                            if image_obj:
                                normalized_bytes, filename = self._prepare_image_asset(
                                    image_obj, page_num=page_num, image_index=img_idx + 1
                                )
                                embedded_images.append((filename, normalized_bytes))
                                parts.append(
                                    f"<!-- Page {page_num} Image {img_idx + 1} -->\n"
                                    f"![Page {page_num} Image {img_idx + 1}]"
                                    f"({self.IMAGE_PLACEHOLDER_SCHEME}{filename})"
                                )
                                meta["images_extracted"] += 1
                        except Exception as img_err:
                            logger.warning(
                                f"Failed to extract image {img_idx + 1} on page {page_num}: {img_err}"
                            )

                # Note: bookmarks with unresolvable page numbers are injected at page 1

            if not parts:
                logger.warning(f"No content extracted from {pdf_path}")
                return "", meta, embedded_images

            markdown_content = "\n\n".join(parts)
            logger.info(
                f"Local conversion: {meta['pages_processed']}/{meta['total_pages']} pages, "
                f"{meta['bookmarks_found']} bookmarks ({meta['heading_source']}), "
                f"{meta['images_extracted']} images, {meta['tables_extracted']} tables → "
                f"{len(markdown_content)} chars"
            )

            return markdown_content, meta, embedded_images

        except Exception as e:
            logger.error(f"pdfplumber conversion failed: {e}")
            raise

    def _extract_bookmarks(self, pdf) -> List[Dict[str, Any]]:
        """Extract bookmark structure from PDF outlines.

        Returns: [{level: int, title: str, page_num: int(1-based)}]
        """
        try:
            if not hasattr(pdf, "doc") or not hasattr(pdf.doc, "get_outlines"):
                return []

            outlines = pdf.doc.get_outlines()
            if not outlines:
                return []

            # Build objid → page_number mapping
            objid_to_num = {
                page.page_obj.objid: i + 1
                for i, page in enumerate(pdf.pages)
                if hasattr(page, "page_obj") and hasattr(page.page_obj, "objid")
            }

            bookmarks = []
            for level, title, dest, _action, _se in outlines:
                if not title or not title.strip():
                    continue

                page_num = None
                try:
                    if dest and len(dest) > 0:
                        page_ref = dest[0]
                        if hasattr(page_ref, "objid"):
                            page_num = objid_to_num.get(page_ref.objid)
                        elif hasattr(page_ref, "resolve"):
                            resolved = page_ref.resolve()
                            if hasattr(resolved, "objid"):
                                page_num = objid_to_num.get(resolved.objid)
                        elif isinstance(page_ref, int):
                            # 0-based integer page index (common in many PDF producers)
                            candidate = page_ref + 1
                            if 1 <= candidate <= len(pdf.pages):
                                page_num = candidate
                except Exception:
                    pass

                bookmarks.append(
                    {
                        "level": min(max(level, 1), 6),
                        "title": title.strip(),
                        "page_num": page_num,
                    }
                )

            return bookmarks

        except Exception as e:
            logger.warning(f"Failed to extract bookmarks: {e}")
            return []

    def _detect_headings_by_font(self, pdf) -> List[Dict[str, Any]]:
        """Detect headings by font size analysis.

        Returns: [{level: int, title: str, page_num: int(1-based)}]
        """
        try:
            # Step 1: Sample font size distribution (every 5th page)
            size_counter: Counter = Counter()
            sample_pages = pdf.pages[::5]
            for page in sample_pages:
                for char in page.chars:
                    if char["text"].strip():
                        rounded = round(char["size"] * 2) / 2
                        size_counter[rounded] += 1

            if not size_counter:
                return []

            # Step 2: Determine body font size and heading font sizes
            body_size = size_counter.most_common(1)[0][0]
            min_delta = self.config.font_heading_min_delta

            heading_sizes = sorted(
                [
                    s
                    for s, count in size_counter.items()
                    if s >= body_size + min_delta and count < size_counter[body_size] * 0.5
                ],
                reverse=True,
            )

            max_levels = self.config.max_heading_levels
            heading_sizes = heading_sizes[:max_levels]

            if not heading_sizes:
                logger.debug(f"Font analysis: body_size={body_size}pt, no heading sizes found")
                return []

            size_to_level = {s: i + 1 for i, s in enumerate(heading_sizes)}
            logger.debug(
                f"Font analysis: body_size={body_size}pt, "
                f"heading_sizes={heading_sizes}, size_to_level={size_to_level}"
            )

            # Step 3: Extract heading text page by page
            headings: List[Dict[str, Any]] = []

            def flush_line(chars_to_flush: list, page_num: int) -> None:
                if not chars_to_flush:
                    return
                title = "".join(c["text"] for c in chars_to_flush).strip()
                size = round(chars_to_flush[0]["size"] * 2) / 2

                if len(title) < 2:
                    return
                if len(title) > 100:
                    return
                if title.isdigit():
                    return
                if re.match(r"^[\d\s.·…]+$", title):
                    return

                headings.append(
                    {
                        "level": size_to_level[size],
                        "title": title,
                        "page_num": page_num,
                    }
                )

            for page in pdf.pages:
                page_num = page.page_number + 1
                chars = sorted(page.chars, key=lambda c: (c["top"], c["x0"]))

                current_line_chars: list = []
                current_top = None

                for char in chars:
                    # Performance: headings won't appear in bottom 70% of page
                    if char["top"] > page.height * 0.3:
                        flush_line(current_line_chars, page_num)
                        current_line_chars = []
                        break

                    rounded_size = round(char["size"] * 2) / 2
                    if rounded_size not in size_to_level:
                        flush_line(current_line_chars, page_num)
                        current_line_chars = []
                        current_top = None
                        continue

                    # Same line check (top offset < 2pt)
                    if current_top is not None and abs(char["top"] - current_top) > 2:
                        flush_line(current_line_chars, page_num)
                        current_line_chars = []

                    current_line_chars.append(char)
                    current_top = char["top"]

                flush_line(current_line_chars, page_num)

            # Step 4: Deduplicate - filter headers appearing on >30% of pages
            title_page_count: Counter = Counter(h["title"] for h in headings)
            total_pages = len(pdf.pages)
            header_titles = {t for t, c in title_page_count.items() if c > total_pages * 0.3}
            headings = [h for h in headings if h["title"] not in header_titles]

            logger.debug(
                f"Font heading detection: {len(headings)} headings found "
                f"(filtered {len(header_titles)} header titles)"
            )
            return headings

        except Exception as e:
            logger.warning(f"Failed to detect headings by font: {e}")
            return []

    def _extract_image_from_page(self, page, img_info: dict) -> Optional[bytes]:
        """
        Extract image data from PDF page.

        Args:
            page: pdfplumber page object
            img_info: Image metadata from page.images

        Returns:
            Image bytes or None if extraction fails
        """
        try:
            stream = img_info.get("stream") if isinstance(img_info, dict) else None
            if stream and hasattr(stream, "get_data"):
                data = stream.get_data()
                if data:
                    return data

            if hasattr(page, "page_obj") and hasattr(page.page_obj, "resources"):
                resources = page.page_obj.resources
                if resources and "XObject" in resources:
                    xobjects = resources["XObject"]
                    for obj_name in xobjects:
                        obj = xobjects[obj_name]
                        if hasattr(obj, "resolve"):
                            resolved = obj.resolve()
                            if resolved.get("Subtype") and resolved["Subtype"].name == "Image":
                                data = resolved.get("stream")
                                if data:
                                    return data.get_data()

            return None

        except Exception as e:
            logger.debug(f"Image extraction error: {e}")
            return None

    async def _convert_mineru(
        self, pdf_path: Path
    ) -> tuple[str, Dict[str, Any], list[tuple[str, bytes]]]:
        """
        Convert PDF to Markdown using MinerU API.

        Args:
            pdf_path: Path to PDF file

        Returns:
            Tuple of (markdown_content, metadata, extracted_images)

        Raises:
            ImportError: If httpx not installed
            Exception: If API call fails
        """
        httpx = lazy_import("httpx")

        if not self.config.mineru_endpoint:
            raise ValueError("MinerU endpoint not configured")

        meta = {
            "strategy": "mineru",
            "endpoint": self.config.mineru_endpoint,
            "api_version": None,
        }

        try:
            async with httpx.AsyncClient(timeout=self.config.mineru_timeout) as client:
                # Prepare file upload
                with open(pdf_path, "rb") as f:
                    files = {"file": (pdf_path.name, f, "application/pdf")}

                    # Prepare headers
                    headers = {}
                    if self.config.mineru_api_key:
                        headers["Authorization"] = f"Bearer {self.config.mineru_api_key}"

                    # Prepare request params
                    params = self.config.mineru_params or {}

                    # Make API request
                    logger.info(f"Calling MinerU API: {self.config.mineru_endpoint}")
                    response = await client.post(
                        self.config.mineru_endpoint,
                        files=files,
                        headers=headers,
                        params=params,
                    )
                    response.raise_for_status()

                # Parse response
                result = response.json()
                markdown_content = result.get("markdown", "")

                # Extract metadata from response
                meta["api_version"] = result.get("version")
                meta["processing_time"] = result.get("processing_time")
                meta["total_pages"] = result.get("total_pages")

                if not markdown_content:
                    logger.warning(f"MinerU returned empty content for {pdf_path}")

                logger.info(
                    f"MinerU conversion: {meta.get('total_pages', '?')} pages → "
                    f"{len(markdown_content)} chars"
                )

                return markdown_content, meta, []

        except Exception as e:
            logger.error(f"MinerU API call failed: {e}")
            raise

    def _prepare_image_asset(
        self,
        image_bytes: bytes,
        *,
        page_num: int,
        image_index: int,
    ) -> tuple[bytes, str]:
        """Best-effort normalize extracted PDF image bytes into a stable asset file."""
        stem = f"page{page_num}_img{image_index}"
        try:
            with Image.open(io.BytesIO(image_bytes)) as img:
                format_name = (img.format or "PNG").upper()
                extension = {
                    "JPEG": ".jpg",
                    "JPG": ".jpg",
                    "PNG": ".png",
                    "WEBP": ".webp",
                    "GIF": ".gif",
                    "BMP": ".bmp",
                    "TIFF": ".tiff",
                }.get(format_name, ".png")
                return image_bytes, f"{stem}{extension}"
        except Exception:
            # Keep the original bytes when format probing fails so we do not drop
            # potentially useful image assets during PDF ingestion.
            return image_bytes, f"{stem}.png"

    async def _write_embedded_images(
        self, temp_uri: Optional[str], image_entries: list[tuple[str, bytes]]
    ) -> list[str]:
        """Persist extracted PDF images under the parsed document root."""
        if not temp_uri or not image_entries:
            return []

        viking_fs = self._get_viking_fs()
        entries = await viking_fs.ls(temp_uri)
        doc_dirs = [e for e in entries if e.get("isDir") and e["name"] not in {".", ".."}]

        if len(doc_dirs) != 1:
            logger.warning(
                f"[PDFParser] Expected 1 document directory in {temp_uri}, found {len(doc_dirs)}"
            )
            return []

        doc_root_uri = f"{temp_uri}/{doc_dirs[0]['name']}"
        images_dir_uri = f"{doc_root_uri}/{self.EMBEDDED_IMAGES_DIR}"
        await viking_fs.mkdir(images_dir_uri, exist_ok=True)

        written_images: list[str] = []
        for filename, image_data in image_entries:
            await viking_fs.write(f"{images_dir_uri}/{filename}", image_data)
            written_images.append(filename)

        return written_images

    def _format_table_markdown(self, table: List[List[Optional[str]]]) -> str:
        """
        Convert table data to Markdown table format.

        Args:
            table: 2D array of table cells

        Returns:
            Markdown table string

        Examples:
            >>> table = [["Name", "Age"], ["Alice", "30"], ["Bob", "25"]]
            >>> print(parser._format_table_markdown(table))
            | Name | Age |
            | --- | --- |
            | Alice | 30 |
            | Bob | 25 |
        """
        if not table or not table[0]:
            return ""

        # Clean cells and handle None values
        def clean_cell(cell):
            if cell is None:
                return ""
            return str(cell).strip().replace("|", "\\|")  # Escape pipe characters

        lines = []

        # Header row
        header = table[0]
        header_cells = [clean_cell(cell) for cell in header]
        lines.append("| " + " | ".join(header_cells) + " |")

        # Separator row
        separator = ["---"] * len(header)
        lines.append("| " + " | ".join(separator) + " |")

        # Data rows
        for row in table[1:]:
            # Pad row to match header length
            padded_row = row + [None] * (len(header) - len(row))
            cells = [clean_cell(cell) for cell in padded_row[: len(header)]]
            lines.append("| " + " | ".join(cells) + " |")

        return "\n".join(lines)

    async def parse_content(
        self, content: str, source_path: Optional[str] = None, instruction: str = "", **kwargs
    ) -> ParseResult:
        """
        Parse PDF content string.

        Note: This method is not recommended for PDFParser as it requires
        file path for conversion tools. Use parse() with file path instead.

        Args:
            content: PDF content (not supported)
            source_path: Optional source path
            **kwargs: Additional options

        Raises:
            NotImplementedError: PDFParser requires file path
        """
        raise NotImplementedError(
            "PDFParser does not support parsing content strings. "
            "Use parse() with a file path instead."
        )
