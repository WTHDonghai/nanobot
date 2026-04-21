"""Bid-material retrieval services and shared evidence models."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from vikingbot.openviking_mount.ov_server import VikingClient
from vikingbot.openviking_mount.uri_utils import is_generic_scope_summary_uri, is_summary_uri
from vikingbot.utils.helpers import get_mcp_artifacts_path, stage_local_image_for_send

DEFAULT_TARGET_URI = "viking://resources/"
MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")
MARKDOWN_IMAGE_CAPTURE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
CERTIFICATE_QUERY_TERMS = (
    "资质",
    "证书",
    "证照",
    "证件",
    "许可证",
    "营业执照",
    "授权书",
    "认证",
    "license",
    "licence",
    "certificate",
    "certification",
    "permit",
    "authorization",
)


class ImageArtifact(BaseModel):
    """Materialized image evidence."""

    source_uri: str
    local_path: str
    caption: str | None = None
    page_hint: str | None = None


class EvidenceItem(BaseModel):
    """One reusable piece of bid evidence."""

    id: str
    kind: str
    title: str
    source_uri: str
    source_document_uri: str | None = None
    score: float = 0.0
    reason: str = ""
    excerpt: str | None = None
    images: list[ImageArtifact] = Field(default_factory=list)
    content_markdown: str = ""


class ClaimCoverage(BaseModel):
    """Coverage status for one bid-writing claim."""

    claim: str
    status: Literal["supported", "partial", "missing"]
    evidence_item_ids: list[str] = Field(default_factory=list)
    notes: str = ""


class EvidencePack(BaseModel):
    """Structured evidence returned to downstream agents."""

    intent: Literal["certificate", "solution", "section_evidence"]
    query: str
    target_uri: str = DEFAULT_TARGET_URI
    summary: str
    items: list[EvidenceItem] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    claims: list[ClaimCoverage] | None = None


def render_evidence_pack_for_agent(pack: EvidencePack) -> str:
    """Render one evidence pack into agent-friendly text plus sendable images."""
    lines = [
        f"Intent: {pack.intent}",
        f"Query: {pack.query}",
        f"Target URI: {pack.target_uri}",
        f"Summary: {pack.summary}",
        f"Evidence items: {len(pack.items)}",
    ]

    if pack.gaps:
        lines.append("Gaps:")
        lines.extend(f"- {gap}" for gap in pack.gaps)

    if pack.claims:
        lines.append("Claims:")
        for claim in pack.claims:
            lines.append(
                f"- [{claim.status}] {claim.claim} | evidence={', '.join(claim.evidence_item_ids) or 'none'}"
            )
            if claim.notes:
                lines.append(f"  Notes: {claim.notes}")

    if not pack.items:
        lines.append("Items: none")
        return "\n".join(lines)

    lines.append("Items:")
    for item in pack.items:
        lines.append(f"- {item.id} | title={item.title} | source={item.source_document_uri or item.source_uri}")
        content_markdown = BidMaterialService._stage_markdown_local_images_for_send(
            item.content_markdown
        )
        if content_markdown:
            lines.append("  Content:")
            lines.extend(f"  {line}" if line else "  " for line in content_markdown.splitlines())

    return "\n".join(lines)


class BidMaterialService:
    """Shared service for bid-material retrieval and evidence packing."""

    def __init__(
        self,
        client_factory: Callable[[str | None], Awaitable[VikingClient]] | None = None,
        artifact_root: Path | None = None,
        agent_id: str | None = None,
    ) -> None:
        self._client_factory = client_factory or VikingClient.create
        self._artifact_root = artifact_root
        self._agent_id = agent_id

    async def search_certificates(
        self,
        query: str,
        target_uri: str = DEFAULT_TARGET_URI,
        top_k: int = 5,
        *,
        context_id: str | None = None,
    ) -> EvidencePack:
        """Search certificate-style evidence with image-first ordering."""
        client = await self._client_factory(context_id or self._agent_id)
        try:
            results = await client.search(query, target_uri=target_uri)
            ranked_resources = self._rank_certificate_resources(results.get("resources") or [])
            items: list[EvidenceItem] = []
            gaps: list[str] = []

            for resource in ranked_resources:
                if len(items) >= top_k:
                    break
                item = await self._build_certificate_item(client, query, resource)
                if item is not None:
                    items.append(item)

            if not items:
                if any(is_summary_uri(str(r.get("uri", ""))) for r in ranked_resources):
                    gaps.append("Only summary-level matches were found; no concrete certificate file was confirmed.")
                else:
                    gaps.append("No certificate image or supporting document was found in the current scope.")

            summary = self._build_summary(
                query=query,
                items=items,
                gaps=gaps,
                success_text="Found reusable certificate evidence with materialized image files.",
                empty_text="No directly reusable certificate evidence was found.",
            )
            return EvidencePack(
                intent="certificate",
                query=query,
                target_uri=target_uri or DEFAULT_TARGET_URI,
                summary=summary,
                items=items,
                gaps=gaps,
            )
        finally:
            await client.close()

    async def search_solution_materials(
        self,
        query: str,
        target_uri: str = DEFAULT_TARGET_URI,
        top_k: int = 5,
        *,
        context_id: str | None = None,
    ) -> EvidencePack:
        """Search text-first solution evidence with optional related images."""
        client = await self._client_factory(context_id or self._agent_id)
        try:
            results = await client.search(query, target_uri=target_uri)
            ranked_resources = self._rank_solution_resources(results.get("resources") or [])
            items: list[EvidenceItem] = []
            gaps: list[str] = []

            for resource in ranked_resources:
                if len(items) >= top_k:
                    break
                item = await self._build_solution_item(
                    client,
                    query,
                    resource,
                )
                if item is not None:
                    items.append(item)

            if not items:
                if any(is_summary_uri(str(r.get("uri", ""))) for r in ranked_resources):
                    gaps.append("Only summary matches were found; no concrete solution document was confirmed.")
                else:
                    gaps.append("No reusable solution material was found in the current scope.")

            summary = self._build_summary(
                query=query,
                items=items,
                gaps=gaps,
                success_text="Found solution evidence with excerpts and related images when available.",
                empty_text="No directly reusable solution evidence was found.",
            )
            return EvidencePack(
                intent="solution",
                query=query,
                target_uri=target_uri or DEFAULT_TARGET_URI,
                summary=summary,
                items=items,
                gaps=gaps,
            )
        finally:
            await client.close()

    async def collect_bid_evidence(
        self,
        section_name: str,
        requirement: str,
        target_uri: str = DEFAULT_TARGET_URI,
        top_k: int = 8,
        *,
        context_id: str | None = None,
    ) -> EvidencePack:
        """Collect section-level evidence and claim coverage."""
        claims = self._split_claims(requirement)
        if not claims:
            claims = [requirement.strip()] if requirement.strip() else [section_name.strip()]

        aggregated_items: dict[str, EvidenceItem] = {}
        coverages: list[ClaimCoverage] = []
        gaps: list[str] = []

        for claim in claims:
            if self._looks_like_certificate_query(claim):
                claim_pack = await self.search_certificates(
                    query=claim,
                    target_uri=target_uri,
                    top_k=min(top_k, 3),
                    context_id=context_id,
                )
            else:
                claim_pack = await self.search_solution_materials(
                    query=claim,
                    target_uri=target_uri,
                    top_k=min(top_k, 3),
                    context_id=context_id,
                )

            for item in claim_pack.items:
                aggregated_items.setdefault(item.id, item)

            if claim_pack.items and claim_pack.gaps:
                status = "partial"
            elif claim_pack.items:
                status = "supported"
            else:
                status = "missing"

            coverages.append(
                ClaimCoverage(
                    claim=claim,
                    status=status,
                    evidence_item_ids=[item.id for item in claim_pack.items],
                    notes="; ".join(claim_pack.gaps) if claim_pack.gaps else "",
                )
            )
            gaps.extend(claim_pack.gaps)

        items = list(aggregated_items.values())[:top_k]
        missing_claims = [claim.claim for claim in coverages if claim.status == "missing"]
        if missing_claims:
            gaps.append(f"Missing evidence for {len(missing_claims)} claim(s): {'; '.join(missing_claims)}")

        summary = self._build_section_summary(section_name=section_name, coverages=coverages)
        return EvidencePack(
            intent="section_evidence",
            query=f"{section_name}: {requirement}",
            target_uri=target_uri or DEFAULT_TARGET_URI,
            summary=summary,
            items=items,
            gaps=list(dict.fromkeys(gaps)),
            claims=coverages,
        )

    async def read_resource_markdown(
        self,
        uri: str,
        *,
        context_id: str | None = None,
    ) -> str:
        """Read one resource as markdown with local image links for MCP consumers."""
        client = await self._client_factory(context_id or self._agent_id)
        try:
            normalized_uri = str(uri or "").strip()
            if not normalized_uri:
                return ""

            stat = await client.stat(normalized_uri)
            if not stat:
                return ""

            if self._is_image_read_target(normalized_uri, stat):
                images = await self._materialize_uri_images(client, normalized_uri)
                return self._render_local_images(images)

            read_uri = normalized_uri.rstrip("/")
            if stat.get("isDir"):
                resolved_uri, candidate_uris = await client.resolve_read_uri(read_uri)
                if not resolved_uri:
                    if candidate_uris:
                        candidates = "\n".join(
                            f"{index}. {candidate_uri}"
                            for index, candidate_uri in enumerate(candidate_uris[:8], start=1)
                        )
                        return (
                            f"目录资源 {normalized_uri} 不能直接读取正文。"
                            "请改为读取以下 URI 之一：\n"
                            f"{candidates}"
                        )
                    return f"目录资源 {normalized_uri} 下没有可读取的正文文件。"
                read_uri = resolved_uri

            content = await client.read_content(read_uri, level="read")
            if not content:
                return ""

            content = await client.materialize_inline_image_refs_to_directory(
                content,
                read_uri,
                self._artifact_dir(),
            )
            if MARKDOWN_IMAGE_RE.search(content):
                return content

            images = await self._materialize_related_images(client, read_uri)
            if not images:
                return content

            return f"{content}\n\n---\n\n相关图片：\n{self._render_local_images(images)}"
        finally:
            await client.close()

    def _artifact_dir(self) -> Path:
        root = self._artifact_root or get_mcp_artifacts_path("bid-material")
        root.mkdir(parents=True, exist_ok=True)
        return root

    async def _build_certificate_item(
        self,
        client: VikingClient,
        query: str,
        resource: dict[str, Any],
    ) -> EvidenceItem | None:
        uri = str(resource.get("uri", "") or "").strip()
        if not uri or is_generic_scope_summary_uri(uri):
            return None

        if self._is_image_uri(uri):
            images = await self._materialize_uri_images(client, uri)
            if not images:
                return None
            images = self._prefer_single_image_caption(
                images,
                preferred_caption=str(resource.get("match_reason") or "").strip(),
            )
            excerpt = await self._read_source_document_excerpt(client, uri, query)
            item = self._make_evidence_item(
                kind="image",
                resource=resource,
                source_uri=uri,
                source_document_uri=self._infer_source_document_uri(uri),
                excerpt=excerpt,
                images=images,
            )
            item.content_markdown = self._compose_content_markdown(excerpt=excerpt, images=images)
            return item

        if is_summary_uri(uri):
            return None

        source_document_uri = await self._resolve_document_uri(client, uri)
        if not source_document_uri:
            return None

        content = await client.read_content(source_document_uri, level="read")
        if not content:
            return None

        excerpt = self._extract_excerpt(content, query=query)
        images = await self._materialize_document_images(
            client,
            source_document_uri,
            content=content,
            query=query,
        )
        if not images:
            return None

        item = self._make_evidence_item(
            kind="document",
            resource=resource,
            source_uri=uri,
            source_document_uri=source_document_uri,
            excerpt=excerpt,
            images=images,
        )
        item.content_markdown = await self._build_document_content_markdown(
            client,
            source_document_uri,
            content=content,
            query=query,
            images=images,
            excerpt=excerpt,
        )
        return item

    async def _build_solution_item(
        self,
        client: VikingClient,
        query: str,
        resource: dict[str, Any],
    ) -> EvidenceItem | None:
        uri = str(resource.get("uri", "") or "").strip()
        if not uri or is_generic_scope_summary_uri(uri) or is_summary_uri(uri):
            return None

        if self._is_image_uri(uri):
            images = await self._materialize_uri_images(client, uri)
            if not images:
                return None
            images = self._prefer_single_image_caption(
                images,
                preferred_caption=str(resource.get("match_reason") or "").strip(),
            )
            source_document_uri = self._infer_source_document_uri(uri)
            excerpt = await self._read_source_document_excerpt(client, uri, query)
            item = self._make_evidence_item(
                kind="image",
                resource=resource,
                source_uri=uri,
                source_document_uri=source_document_uri,
                excerpt=excerpt,
                images=images,
            )
            item.content_markdown = self._compose_content_markdown(excerpt=excerpt, images=images)
            return item

        source_document_uri = await self._resolve_document_uri(client, uri)
        if not source_document_uri:
            return None

        content = await client.read_content(source_document_uri, level="read")
        if not content:
            return None

        excerpt = self._extract_excerpt(content, query=query)
        images = await self._materialize_document_images(
            client,
            source_document_uri,
            content=content,
            query=query,
        )
        item = self._make_evidence_item(
            kind="document",
            resource=resource,
            source_uri=uri,
            source_document_uri=source_document_uri,
            excerpt=excerpt,
            images=images,
        )
        item.content_markdown = await self._build_document_content_markdown(
            client,
            source_document_uri,
            content=content,
            query=query,
            images=images,
            excerpt=excerpt,
        )
        return item

    def _make_evidence_item(
        self,
        *,
        kind: str,
        resource: dict[str, Any],
        source_uri: str,
        source_document_uri: str | None,
        excerpt: str | None,
        images: list[ImageArtifact],
    ) -> EvidenceItem:
        title = self._make_title(source_document_uri or source_uri)
        return EvidenceItem(
            id=self._stable_item_id(source_uri),
            kind=kind,
            title=title,
            source_uri=source_uri,
            source_document_uri=source_document_uri,
            score=self._score(resource),
            reason=str(resource.get("match_reason") or "").strip(),
            excerpt=excerpt,
            images=images,
            content_markdown=self._compose_content_markdown(excerpt=excerpt, images=images),
        )

    async def _read_excerpt(self, client: VikingClient, uri: str, query: str) -> str | None:
        content = await client.read_content(uri, level="read")
        if not content:
            return None
        return self._extract_excerpt(content, query=query)

    async def _read_source_document_excerpt(
        self,
        client: VikingClient,
        image_uri: str,
        query: str,
    ) -> str | None:
        source_document_uri = self._infer_source_document_uri(image_uri)
        if not source_document_uri:
            return None
        resolved = await self._resolve_document_uri(client, source_document_uri)
        if not resolved:
            return None
        return await self._read_excerpt(client, resolved, query)

    async def _resolve_document_uri(self, client: VikingClient, uri: str) -> str | None:
        resolved_uri, _candidate_uris = await client.resolve_read_uri(uri)
        return resolved_uri

    async def _materialize_document_images(
        self,
        client: VikingClient,
        uri: str,
        *,
        content: str,
        query: str,
    ) -> list[ImageArtifact]:
        relevant_fragment = self._extract_relevant_fragment(content, query=query)
        for fragment in self._ordered_image_fragments(relevant_fragment, content):
            image_files = await client.export_referenced_images(
                fragment,
                uri,
                output_dir=self._artifact_dir(),
                max_images=None,
            )
            if image_files:
                return [self._to_image_artifact(image_file) for image_file in image_files]

        return await self._materialize_related_images(client, uri)

    @staticmethod
    def _ordered_image_fragments(primary_fragment: str, full_content: str) -> list[str]:
        fragments: list[str] = []
        for candidate in (primary_fragment, full_content):
            normalized = str(candidate or "").strip()
            if normalized and normalized not in fragments:
                fragments.append(normalized)
        return fragments

    async def _materialize_related_images(
        self,
        client: VikingClient,
        uri: str,
    ) -> list[ImageArtifact]:
        image_files = await client.export_related_images(
            uri,
            output_dir=self._artifact_dir(),
            max_images=None,
        )
        return [self._to_image_artifact(image_file) for image_file in image_files]

    async def _materialize_uri_images(
        self,
        client: VikingClient,
        uri: str,
    ) -> list[ImageArtifact]:
        image_files = await client.export_uri_images(
            uri,
            output_dir=self._artifact_dir(),
            max_images=None,
        )
        return [self._to_image_artifact(image_file) for image_file in image_files]

    @staticmethod
    def _to_image_artifact(image_file: dict[str, str]) -> ImageArtifact:
        return ImageArtifact(
            source_uri=str(image_file.get("source_uri") or ""),
            local_path=str(image_file.get("local_path") or ""),
            caption=str(image_file.get("caption") or "") or None,
            page_hint=str(image_file.get("page_hint") or "") or None,
        )

    @classmethod
    def _prefer_single_image_caption(
        cls,
        images: list[ImageArtifact],
        *,
        preferred_caption: str,
    ) -> list[ImageArtifact]:
        normalized_preferred = str(preferred_caption or "").strip()
        if len(images) != 1 or not normalized_preferred:
            return images

        image = images[0]
        current_caption = str(image.caption or "").strip()
        if current_caption and not cls._should_upgrade_image_caption(
            current_caption=current_caption,
            preferred_caption=normalized_preferred,
        ):
            return images

        updated_image = image.model_copy(update={"caption": normalized_preferred})
        return [updated_image]

    @staticmethod
    def _should_upgrade_image_caption(
        *,
        current_caption: str,
        preferred_caption: str,
    ) -> bool:
        current = current_caption.strip()
        preferred = preferred_caption.strip()
        if not current:
            return True
        current_lower = current.lower()
        if re.fullmatch(r"(image|img|figure|page)[_\-\s]?\d*(?:[_\-\s]?img\d+)?", current_lower):
            return True
        return preferred.startswith(current) and preferred != current

    @staticmethod
    def _render_local_images(images: list[ImageArtifact]) -> str:
        return "\n".join(
            f"![{(image.caption or Path(image.local_path).stem or 'image').strip()}]({image.local_path})"
            for image in images
            if str(image.local_path or "").strip()
        )

    @classmethod
    def _compose_content_markdown(
        cls,
        *,
        excerpt: str | None,
        images: list[ImageArtifact],
        fragment_markdown: str | None = None,
    ) -> str:
        fragment = str(fragment_markdown or "").strip()
        if fragment:
            return fragment

        parts: list[str] = []
        excerpt_text = str(excerpt or "").strip()
        if excerpt_text:
            parts.append(excerpt_text)

        image_block = cls._render_local_images(images)
        if image_block:
            parts.append(image_block)

        return "\n\n".join(part for part in parts if part).strip()

    async def _build_document_content_markdown(
        self,
        client: VikingClient,
        uri: str,
        *,
        content: str,
        query: str,
        images: list[ImageArtifact],
        excerpt: str | None,
    ) -> str:
        relevant_fragment = self._extract_relevant_fragment(content, query=query)
        if relevant_fragment:
            rendered_fragment = await client.materialize_inline_image_refs_to_directory(
                relevant_fragment,
                uri,
                self._artifact_dir(),
            )
            normalized_fragment = self._normalize_fragment_markdown(rendered_fragment)
            if MARKDOWN_IMAGE_RE.search(normalized_fragment):
                return normalized_fragment

        return self._compose_content_markdown(excerpt=excerpt, images=images)

    @staticmethod
    def _normalize_fragment_markdown(content: str) -> str:
        lines = [line.rstrip() for line in str(content or "").splitlines()]
        normalized: list[str] = []
        previous_blank = False
        for line in lines:
            if not line.strip():
                if normalized and not previous_blank:
                    normalized.append("")
                previous_blank = True
                continue
            normalized.append(line)
            previous_blank = False
        while normalized and not normalized[-1].strip():
            normalized.pop()
        return "\n".join(normalized).strip()

    @staticmethod
    def _stage_markdown_local_images_for_send(content: str) -> str:
        rendered = str(content or "")

        def replace(match: re.Match[str]) -> str:
            alt_text = match.group(1)
            image_ref = match.group(2).strip()
            if not image_ref or "://" in image_ref:
                return match.group(0)

            try:
                local_path = Path(image_ref).expanduser().resolve()
            except Exception:
                return match.group(0)
            if not local_path.exists() or not local_path.is_file():
                return match.group(0)

            return f"![{alt_text}]({stage_local_image_for_send(local_path)})"

        return MARKDOWN_IMAGE_CAPTURE_RE.sub(replace, rendered)

    @staticmethod
    def _make_title(uri: str) -> str:
        name = Path(uri.rstrip("/")).stem.strip()
        return name or uri.rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def _stable_item_id(uri: str) -> str:
        digest = hashlib.sha1(uri.encode("utf-8")).hexdigest()[:12]
        return f"evi_{digest}"

    @staticmethod
    def _score(resource: dict[str, Any]) -> float:
        try:
            return float(resource.get("score", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _rank_certificate_resources(cls, resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            resources,
            key=lambda resource: (
                cls._certificate_rank(resource),
                -cls._score(resource),
                str(resource.get("uri", "")),
            ),
        )

    @classmethod
    def _rank_solution_resources(cls, resources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            resources,
            key=lambda resource: (
                cls._solution_rank(resource),
                -cls._score(resource),
                str(resource.get("uri", "")),
            ),
        )

    @classmethod
    def _certificate_rank(cls, resource: dict[str, Any]) -> int:
        uri = str(resource.get("uri", "") or "")
        if cls._is_image_uri(uri):
            return 0
        if not is_summary_uri(uri):
            return 1
        return 2

    @classmethod
    def _solution_rank(cls, resource: dict[str, Any]) -> int:
        uri = str(resource.get("uri", "") or "")
        if not cls._is_image_uri(uri) and not is_summary_uri(uri):
            return 0
        if cls._is_image_uri(uri):
            return 1
        return 2

    @staticmethod
    def _is_image_uri(uri: str) -> bool:
        return Path(uri.rstrip("/")).suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".svg",
            ".tiff",
        }

    @classmethod
    def _is_image_read_target(cls, uri: str, stat: dict[str, Any]) -> bool:
        normalized_uri = uri.rstrip("/")
        if stat.get("isDir"):
            return normalized_uri.endswith("/_images") or normalized_uri.endswith("_images")
        return cls._is_image_uri(normalized_uri)

    @staticmethod
    def _infer_source_document_uri(uri: str) -> str | None:
        normalized = uri.rstrip("/")
        if "/_images/" not in normalized:
            return None
        prefix = normalized.split("/_images/", 1)[0]
        return prefix if prefix else None

    @classmethod
    def _looks_like_certificate_query(cls, query: str) -> bool:
        normalized = (query or "").strip().lower()
        if not normalized:
            return False
        return any(term in query for term in CERTIFICATE_QUERY_TERMS) or any(
            term in normalized for term in CERTIFICATE_QUERY_TERMS
        )

    @classmethod
    def _extract_excerpt(cls, content: str, *, query: str, max_chars: int = 320) -> str:
        content_without_images = MARKDOWN_IMAGE_RE.sub(" ", content or "")
        normalized_content = re.sub(r"\s+", " ", content_without_images).strip()
        if not normalized_content:
            return ""

        keywords = [token for token in re.split(r"[\s,，。；;、]+", query or "") if len(token) >= 2]
        candidates = [segment.strip() for segment in re.split(r"(?<=[。！？.!?])\s+", normalized_content) if segment.strip()]
        for segment in candidates:
            if any(keyword.lower() in segment.lower() for keyword in keywords):
                return cls._truncate(segment, max_chars)
        return cls._truncate(candidates[0] if candidates else normalized_content, max_chars)

    @classmethod
    def _extract_relevant_fragment(cls, content: str, *, query: str, window_before: int = 8, window_after: int = 36) -> str:
        lines = [line.rstrip() for line in (content or "").splitlines()]
        if not lines:
            return ""

        keywords = [token.lower() for token in re.split(r"[\s,，。；;、]+", query or "") if len(token) >= 2]
        scored_indexes = [
            (index, sum(keyword in line.lower() for keyword in keywords))
            for index, line in enumerate(lines)
        ]
        best_index = next(
            (
                index
                for index, score in sorted(scored_indexes, key=lambda item: item[1], reverse=True)
                if score > 0
            ),
            None,
        )
        if best_index is None:
            best_index = next((index for index, line in enumerate(lines) if line.strip()), 0)

        start = max(0, best_index - window_before)
        end = min(len(lines), best_index + window_after)
        image_indexes = [
            index
            for index, line in enumerate(lines)
            if "ov-asset://" in line or ("![" in line and "viking://" in line)
        ]
        nearby_images = [index for index in image_indexes if start - 2 <= index <= end + 2]
        if nearby_images:
            start = max(0, min(start, min(nearby_images) - 1))
            end = min(len(lines), max(end, max(nearby_images) + 2))

        fragment = "\n".join(lines[start:end]).strip()
        return fragment or (content or "").strip()

    @staticmethod
    def _truncate(text: str, max_chars: int) -> str:
        cleaned = re.sub(r"\s+", " ", text or "").strip()
        if len(cleaned) <= max_chars:
            return cleaned
        return f"{cleaned[: max_chars - 3].rstrip()}..."

    @classmethod
    def _split_claims(cls, requirement: str) -> list[str]:
        text = (requirement or "").strip()
        if not text:
            return []
        normalized = re.sub(r"[ \t]+", " ", text)
        raw_parts = re.split(r"\n+|[；;]+|(?<=。)|(?<=\.)", normalized)
        claims = [part.strip(" -\t\r\n。.;；") for part in raw_parts if part.strip(" -\t\r\n。.;；")]
        return list(dict.fromkeys(claims))

    @staticmethod
    def _build_summary(
        *,
        query: str,
        items: list[EvidenceItem],
        gaps: list[str],
        success_text: str,
        empty_text: str,
    ) -> str:
        if items:
            return (
                f"{success_text} Query: {query}. Reusable items: {len(items)}. "
                "Draft directly from each item's content_markdown and keep source_uri for traceability; "
                "do not use this summary itself as final section prose."
            )
        if gaps:
            return f"{empty_text} Query: {query}. Main gap: {gaps[0]}"
        return f"{empty_text} Query: {query}."

    @staticmethod
    def _build_section_summary(section_name: str, coverages: list[ClaimCoverage]) -> str:
        supported = sum(1 for claim in coverages if claim.status == "supported")
        partial = sum(1 for claim in coverages if claim.status == "partial")
        missing = sum(1 for claim in coverages if claim.status == "missing")
        return (
            f"Section '{section_name}' evidence pack prepared. "
            f"Supported: {supported}, partial: {partial}, missing: {missing}. "
            "Expand the section from claims and each evidence item's content_markdown instead of rewriting this summary."
        )

    def pack_to_json(self, pack: EvidencePack) -> str:
        """Serialize an evidence pack for MCP or tool responses."""
        return json.dumps(self.pack_to_dict(pack), indent=2, ensure_ascii=False)

    def pack_to_dict(self, pack: EvidencePack) -> dict[str, Any]:
        """Serialize an evidence pack to a plain dict."""
        payload: dict[str, Any] = {
            "intent": pack.intent,
            "query": pack.query,
            "target_uri": pack.target_uri,
            "summary": pack.summary,
            "items": [
                {
                    "id": item.id,
                    "title": item.title,
                    "source_uri": item.source_document_uri or item.source_uri,
                    "content_markdown": item.content_markdown,
                }
                for item in pack.items
            ],
        }
        if pack.claims is not None:
            payload["claims"] = [claim.model_dump(mode="json") for claim in pack.claims]
        return payload
