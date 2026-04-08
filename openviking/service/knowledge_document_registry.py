"""Persistent registry for user-managed knowledge documents and virtual folders."""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from openviking_cli.exceptions import ConflictError, InvalidArgumentError, NotFoundError
from openviking_cli.utils.uri import VikingURI


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _derive_display_name(source_path: str) -> str:
    parsed = urlparse(source_path)
    if parsed.scheme in {"http", "https", "ssh", "git"} or source_path.startswith("git@"):
        path_name = Path(parsed.path or "").name
        if path_name:
            return path_name
        return parsed.netloc or source_path
    return Path(source_path).name or source_path


def _derive_source_type(source_path: str) -> str:
    parsed = urlparse(source_path)
    if parsed.scheme in {"http", "https"} or source_path.startswith("git@"):
        return "remote"
    if parsed.scheme == "ssh":
        return "remote"
    path = Path(source_path)
    if path.exists():
        return "directory" if path.is_dir() else "file"
    return "unknown"


def _normalize_folder_name(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        raise InvalidArgumentError("Folder name is required.")
    if value in {".", ".."}:
        raise InvalidArgumentError("Folder name is invalid.")
    if "/" in value or "\\" in value:
        raise InvalidArgumentError("Folder name cannot contain path separators.")
    return value


def _normalize_folder_path(folder_path: Optional[str]) -> str:
    if folder_path is None:
        return ""

    raw = str(folder_path).strip().replace("\\", "/")
    if raw in {"", "/"}:
        return ""

    parts: List[str] = []
    for part in raw.split("/"):
        segment = part.strip()
        if not segment or segment == ".":
            continue
        if segment == "..":
            raise InvalidArgumentError("Folder path cannot contain '..'.")
        parts.append(_normalize_folder_name(segment))
    return "/".join(parts)


def _join_folder_path(parent_path: str, name: str) -> str:
    parent = _normalize_folder_path(parent_path)
    child = _normalize_folder_name(name)
    return f"{parent}/{child}" if parent else child


def _resource_prefix_for_folder_path(folder_path: str) -> str:
    segments = [
        VikingURI.sanitize_segment(part)
        for part in _normalize_folder_path(folder_path).split("/")
        if part
    ]
    return VikingURI.build("resources", *segments)


def _replace_resource_folder_prefix(
    resource_root_uri: str,
    old_folder_path: str,
    new_folder_path: str,
) -> str:
    normalized_uri = VikingURI.normalize(resource_root_uri)
    old_prefix = _resource_prefix_for_folder_path(old_folder_path).rstrip("/")
    new_prefix = _resource_prefix_for_folder_path(new_folder_path).rstrip("/")

    if normalized_uri == old_prefix:
        return new_prefix

    old_prefix_with_sep = f"{old_prefix}/"
    if not normalized_uri.startswith(old_prefix_with_sep):
        raise InvalidArgumentError(
            f"Resource URI '{resource_root_uri}' does not match folder path '{old_folder_path}'."
        )

    suffix = normalized_uri[len(old_prefix) :]
    return f"{new_prefix}{suffix}"


DOCUMENT_PROCESSING_STATUS_PROCESSING = "processing"
DOCUMENT_PROCESSING_STATUS_READY = "ready"
DOCUMENT_PROCESSING_STATUS_FAILED = "failed"


def _normalize_processing_status(value: Optional[str]) -> str:
    candidate = str(value or "").strip().lower()
    if candidate in {
        DOCUMENT_PROCESSING_STATUS_PROCESSING,
        DOCUMENT_PROCESSING_STATUS_READY,
        DOCUMENT_PROCESSING_STATUS_FAILED,
    }:
        return candidate
    return DOCUMENT_PROCESSING_STATUS_READY


@dataclass
class KnowledgeFolderRecord:
    """Persistent metadata for a user-managed virtual folder."""

    folder_id: str
    account_id: str
    name: str
    path: str
    parent_path: str
    created_at: str
    updated_at: str
    meta: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeFolderRecord":
        path = _normalize_folder_path(data.get("path"))
        return cls(
            folder_id=str(data["folder_id"]),
            account_id=str(data["account_id"]),
            name=str(data.get("name") or ""),
            path=path,
            parent_path=_normalize_folder_path(data.get("parent_path")),
            created_at=str(data.get("created_at") or _utc_now_iso()),
            updated_at=str(data.get("updated_at") or _utc_now_iso()),
            meta=_json_safe(data.get("meta") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["meta"] = _json_safe(self.meta)
        return payload

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "entry_type": "folder",
            "folder_id": self.folder_id,
            "name": self.name,
            "path": self.path,
            "parent_path": self.parent_path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class KnowledgeDocumentRecord:
    """Persistent metadata for a user-managed knowledge document."""

    document_id: str
    account_id: str
    display_name: str
    source_type: str
    source_ref: str
    resource_root_uri: str
    created_at: str
    updated_at: str
    folder_path: str = ""
    source_format: Optional[str] = None
    reason: str = ""
    instruction: str = ""
    processing_status: str = DOCUMENT_PROCESSING_STATUS_READY
    processing_error: str = ""
    processing_started_at: Optional[str] = None
    processing_completed_at: Optional[str] = None
    original_storage_path: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeDocumentRecord":
        created_at = str(data.get("created_at") or _utc_now_iso())
        updated_at = str(data.get("updated_at") or _utc_now_iso())
        processing_status = _normalize_processing_status(data.get("processing_status"))
        processing_started_at = data.get("processing_started_at") or created_at
        processing_completed_at = data.get("processing_completed_at")
        if processing_status == DOCUMENT_PROCESSING_STATUS_READY and not processing_completed_at:
            processing_completed_at = updated_at
        return cls(
            document_id=str(data["document_id"]),
            account_id=str(data["account_id"]),
            display_name=str(data.get("display_name") or ""),
            source_type=str(data.get("source_type") or "unknown"),
            source_ref=str(data.get("source_ref") or ""),
            resource_root_uri=str(data.get("resource_root_uri") or ""),
            created_at=created_at,
            updated_at=updated_at,
            folder_path=_normalize_folder_path(data.get("folder_path")),
            source_format=data.get("source_format"),
            reason=str(data.get("reason") or ""),
            instruction=str(data.get("instruction") or ""),
            processing_status=processing_status,
            processing_error=str(data.get("processing_error") or ""),
            processing_started_at=processing_started_at,
            processing_completed_at=processing_completed_at,
            original_storage_path=data.get("original_storage_path"),
            meta=_json_safe(data.get("meta") or {}),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["meta"] = _json_safe(self.meta)
        return payload

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "entry_type": "document",
            "document_id": self.document_id,
            "display_name": self.display_name,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "resource_root_uri": self.resource_root_uri,
            "folder_path": self.folder_path,
            "source_format": self.source_format,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "reason": self.reason,
            "instruction": self.instruction,
            "processing_status": self.processing_status,
            "processing_error": self.processing_error,
            "processing_started_at": self.processing_started_at,
            "processing_completed_at": self.processing_completed_at,
            "has_local_copy": bool(self.original_storage_path),
        }


@dataclass
class DocumentMovePlan:
    """Planned knowledge document move within virtual folders."""

    source_document: KnowledgeDocumentRecord
    target_folder_path: str
    updated_document: KnowledgeDocumentRecord


@dataclass
class FolderRenamePlan:
    """Planned virtual folder rename with descendant updates."""

    source_folder: KnowledgeFolderRecord
    renamed_folder: KnowledgeFolderRecord
    original_folders: List[KnowledgeFolderRecord]
    updated_folders: List[KnowledgeFolderRecord]
    original_documents: List[KnowledgeDocumentRecord]
    updated_documents: List[KnowledgeDocumentRecord]
    old_resource_prefix: str
    new_resource_prefix: str


class KnowledgeDocumentRegistry:
    """File-backed registry for knowledge documents and virtual folders."""

    def __init__(self, workspace_path: str):
        self._base_dir = Path(workspace_path).expanduser().resolve() / "knowledge_documents"
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _account_dir(self, account_id: str) -> Path:
        path = self._base_dir / account_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _records_dir(self, account_id: str) -> Path:
        path = self._account_dir(account_id) / "records"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _folders_dir(self, account_id: str) -> Path:
        path = self._account_dir(account_id) / "folders"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _sources_dir(self, account_id: str) -> Path:
        path = self._account_dir(account_id) / "sources"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _record_path(self, account_id: str, document_id: str) -> Path:
        return self._records_dir(account_id) / f"{document_id}.json"

    def _folder_record_path(self, account_id: str, folder_id: str) -> Path:
        return self._folders_dir(account_id) / f"{folder_id}.json"

    def list_documents(self, account_id: str) -> List[KnowledgeDocumentRecord]:
        records: List[KnowledgeDocumentRecord] = []
        for record_path in sorted(self._records_dir(account_id).glob("*.json")):
            with record_path.open("r", encoding="utf-8") as fh:
                records.append(KnowledgeDocumentRecord.from_dict(json.load(fh)))
        records.sort(key=lambda item: (item.folder_path, item.updated_at, item.display_name), reverse=True)
        return records

    def list_folders(self, account_id: str) -> List[KnowledgeFolderRecord]:
        records: List[KnowledgeFolderRecord] = []
        for record_path in sorted(self._folders_dir(account_id).glob("*.json")):
            with record_path.open("r", encoding="utf-8") as fh:
                records.append(KnowledgeFolderRecord.from_dict(json.load(fh)))
        records.sort(key=lambda item: item.path)
        return records

    def get_document(
        self,
        account_id: str,
        document_id: str,
    ) -> Optional[KnowledgeDocumentRecord]:
        record_path = self._record_path(account_id, document_id)
        if not record_path.exists():
            return None
        with record_path.open("r", encoding="utf-8") as fh:
            return KnowledgeDocumentRecord.from_dict(json.load(fh))

    def get_folder(
        self,
        account_id: str,
        folder_id: str,
    ) -> Optional[KnowledgeFolderRecord]:
        record_path = self._folder_record_path(account_id, folder_id)
        if not record_path.exists():
            return None
        with record_path.open("r", encoding="utf-8") as fh:
            return KnowledgeFolderRecord.from_dict(json.load(fh))

    def get_folder_by_path(
        self,
        account_id: str,
        folder_path: str,
    ) -> Optional[KnowledgeFolderRecord]:
        normalized_path = _normalize_folder_path(folder_path)
        if not normalized_path:
            return None
        for record in self.list_folders(account_id):
            if record.path == normalized_path:
                return record
        return None

    def find_by_resource_root_uri(
        self,
        account_id: str,
        resource_root_uri: str,
    ) -> Optional[KnowledgeDocumentRecord]:
        for record in self.list_documents(account_id):
            if record.resource_root_uri == resource_root_uri:
                return record
        return None

    def create_folder(
        self,
        *,
        account_id: str,
        name: str,
        parent_path: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> KnowledgeFolderRecord:
        normalized_parent = _normalize_folder_path(parent_path)
        if normalized_parent and not self.get_folder_by_path(account_id, normalized_parent):
            raise NotFoundError(normalized_parent, "knowledge folder")

        folder_path = _join_folder_path(normalized_parent, name)
        if self.get_folder_by_path(account_id, folder_path):
            raise ConflictError("Folder already exists.", resource=folder_path)

        now = _utc_now_iso()
        record = KnowledgeFolderRecord(
            folder_id=uuid.uuid4().hex,
            account_id=account_id,
            name=_normalize_folder_name(name),
            path=folder_path,
            parent_path=normalized_parent,
            created_at=now,
            updated_at=now,
            meta=_json_safe(meta or {}),
        )
        self._write_folder_record(record)
        return record

    def delete_folder(self, account_id: str, folder_id: str) -> None:
        record = self.get_folder(account_id, folder_id)
        if not record:
            raise NotFoundError(folder_id, "knowledge folder")

        prefix = f"{record.path}/"
        has_subfolders = any(
            folder.folder_id != folder_id and folder.path.startswith(prefix)
            for folder in self.list_folders(account_id)
        )
        has_documents = any(
            document.folder_path == record.path or document.folder_path.startswith(prefix)
            for document in self.list_documents(account_id)
        )
        if has_subfolders or has_documents:
            raise ConflictError("Folder is not empty.", resource=record.path)

        self._folder_record_path(account_id, folder_id).unlink(missing_ok=True)

    def plan_move_document(
        self,
        *,
        account_id: str,
        document_id: str,
        target_folder_path: str = "",
    ) -> DocumentMovePlan:
        record = self.get_document(account_id, document_id)
        if not record:
            raise NotFoundError(document_id, "knowledge document")

        normalized_target_folder = _normalize_folder_path(target_folder_path)
        if normalized_target_folder and not self.get_folder_by_path(account_id, normalized_target_folder):
            raise NotFoundError(normalized_target_folder, "knowledge folder")

        next_resource_root_uri = _replace_resource_folder_prefix(
            record.resource_root_uri,
            record.folder_path,
            normalized_target_folder,
        )
        conflict = self.find_by_resource_root_uri(account_id, next_resource_root_uri)
        if conflict and conflict.document_id != document_id:
            raise ConflictError(
                "A document already exists at the target location.",
                resource=next_resource_root_uri,
            )

        updated_record = replace(
            record,
            folder_path=normalized_target_folder,
            resource_root_uri=next_resource_root_uri,
            updated_at=_utc_now_iso(),
        )
        return DocumentMovePlan(
            source_document=record,
            target_folder_path=normalized_target_folder,
            updated_document=updated_record,
        )

    def apply_document_move(
        self,
        *,
        plan: DocumentMovePlan,
    ) -> KnowledgeDocumentRecord:
        self._write_document_record(plan.updated_document)
        return plan.updated_document

    def plan_rename_folder(
        self,
        *,
        account_id: str,
        folder_id: str,
        new_name: str,
    ) -> FolderRenamePlan:
        source_folder = self.get_folder(account_id, folder_id)
        if not source_folder:
            raise NotFoundError(folder_id, "knowledge folder")

        normalized_new_name = _normalize_folder_name(new_name)
        target_path = _join_folder_path(source_folder.parent_path, normalized_new_name)
        if target_path != source_folder.path:
            existing = self.get_folder_by_path(account_id, target_path)
            if existing and existing.folder_id != folder_id:
                raise ConflictError("Folder already exists.", resource=target_path)

        prefix = f"{source_folder.path}/"
        now = _utc_now_iso()

        original_folders = [
            folder
            for folder in self.list_folders(account_id)
            if folder.path == source_folder.path or folder.path.startswith(prefix)
        ]
        original_documents = [
            document
            for document in self.list_documents(account_id)
            if document.folder_path == source_folder.path or document.folder_path.startswith(prefix)
        ]

        updated_folders: List[KnowledgeFolderRecord] = []
        for folder in original_folders:
            suffix = folder.path[len(source_folder.path) :].lstrip("/")
            next_path = target_path if not suffix else f"{target_path}/{suffix}"
            next_parent_path = next_path.rsplit("/", 1)[0] if "/" in next_path else ""
            next_name = normalized_new_name if folder.folder_id == folder_id else next_path.rsplit("/", 1)[-1]
            updated_folders.append(
                replace(
                    folder,
                    name=next_name,
                    path=next_path,
                    parent_path=next_parent_path,
                    updated_at=now,
                )
            )

        updated_documents: List[KnowledgeDocumentRecord] = []
        seen_resource_uris: Dict[str, str] = {}
        unaffected_documents = {
            document.document_id: document
            for document in self.list_documents(account_id)
            if document.document_id not in {record.document_id for record in original_documents}
        }

        for document in original_documents:
            suffix = document.folder_path[len(source_folder.path) :].lstrip("/")
            next_folder_path = target_path if not suffix else f"{target_path}/{suffix}"
            next_resource_root_uri = _replace_resource_folder_prefix(
                document.resource_root_uri,
                document.folder_path,
                next_folder_path,
            )
            if next_resource_root_uri in seen_resource_uris:
                raise ConflictError(
                    "Two documents would resolve to the same target resource URI.",
                    resource=next_resource_root_uri,
                )
            conflict = self.find_by_resource_root_uri(account_id, next_resource_root_uri)
            if conflict and conflict.document_id not in unaffected_documents:
                conflict = None
            if conflict:
                raise ConflictError(
                    "A document already exists at the target location.",
                    resource=next_resource_root_uri,
                )

            seen_resource_uris[next_resource_root_uri] = document.document_id
            updated_documents.append(
                replace(
                    document,
                    folder_path=next_folder_path,
                    resource_root_uri=next_resource_root_uri,
                    updated_at=now,
                )
            )

        renamed_folder = next(
            folder for folder in updated_folders if folder.folder_id == source_folder.folder_id
        )
        return FolderRenamePlan(
            source_folder=source_folder,
            renamed_folder=renamed_folder,
            original_folders=original_folders,
            updated_folders=updated_folders,
            original_documents=original_documents,
            updated_documents=updated_documents,
            old_resource_prefix=_resource_prefix_for_folder_path(source_folder.path),
            new_resource_prefix=_resource_prefix_for_folder_path(target_path),
        )

    def apply_folder_rename(self, *, plan: FolderRenamePlan) -> KnowledgeFolderRecord:
        try:
            for folder in plan.updated_folders:
                self._write_folder_record(folder)
            for document in plan.updated_documents:
                self._write_document_record(document)
        except Exception:
            for folder in plan.original_folders:
                self._write_folder_record(folder)
            for document in plan.original_documents:
                self._write_document_record(document)
            raise
        return plan.renamed_folder

    def upsert_document(
        self,
        *,
        account_id: str,
        source_path: str,
        source_ref: Optional[str] = None,
        resource_root_uri: str,
        folder_path: Optional[str] = None,
        source_format: Optional[str] = None,
        reason: str = "",
        instruction: str = "",
        processing_status: Optional[str] = None,
        processing_error: Optional[str] = None,
        processing_started_at: Optional[str] = None,
        processing_completed_at: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> KnowledgeDocumentRecord:
        existing = self.find_by_resource_root_uri(account_id, resource_root_uri)
        now = _utc_now_iso()
        document_id = existing.document_id if existing else uuid.uuid4().hex
        created_at = existing.created_at if existing else now

        normalized_folder_path = _normalize_folder_path(
            folder_path if folder_path is not None else (existing.folder_path if existing else "")
        )
        if normalized_folder_path and not self.get_folder_by_path(account_id, normalized_folder_path):
            raise NotFoundError(normalized_folder_path, "knowledge folder")

        original_storage_path = existing.original_storage_path if existing else None
        copied_source = self._copy_source_if_local(account_id, document_id, source_path)
        if copied_source:
            original_storage_path = copied_source

        visible_source_ref = source_ref or source_path
        resolved_processing_status = _normalize_processing_status(
            processing_status if processing_status is not None else (
                existing.processing_status if existing else DOCUMENT_PROCESSING_STATUS_READY
            )
        )
        resolved_processing_error = (
            str(processing_error)
            if processing_error is not None
            else (existing.processing_error if existing else "")
        )
        resolved_processing_started_at = (
            processing_started_at
            if processing_started_at is not None
            else (
                existing.processing_started_at
                if existing and existing.processing_started_at
                else now
            )
        )
        if processing_completed_at is not None:
            resolved_processing_completed_at = processing_completed_at
        elif existing and processing_status is None:
            resolved_processing_completed_at = existing.processing_completed_at
        elif resolved_processing_status == DOCUMENT_PROCESSING_STATUS_READY:
            resolved_processing_completed_at = now
        else:
            resolved_processing_completed_at = None

        record = KnowledgeDocumentRecord(
            document_id=document_id,
            account_id=account_id,
            display_name=_derive_display_name(visible_source_ref),
            source_type=_derive_source_type(source_path),
            source_ref=visible_source_ref,
            resource_root_uri=resource_root_uri,
            created_at=created_at,
            updated_at=now,
            folder_path=normalized_folder_path,
            source_format=source_format,
            reason=reason,
            instruction=instruction,
            processing_status=resolved_processing_status,
            processing_error=resolved_processing_error,
            processing_started_at=resolved_processing_started_at,
            processing_completed_at=resolved_processing_completed_at,
            original_storage_path=original_storage_path,
            meta=_json_safe(meta or {}),
        )
        self._write_document_record(record)
        return record

    def update_document_processing_status(
        self,
        account_id: str,
        document_id: str,
        *,
        processing_status: str,
        processing_error: Optional[str] = None,
        processing_started_at: Optional[str] = None,
        processing_completed_at: Optional[str] = None,
    ) -> Optional[KnowledgeDocumentRecord]:
        record = self.get_document(account_id, document_id)
        if not record:
            return None

        normalized_status = _normalize_processing_status(processing_status)
        now = _utc_now_iso()
        updated_record = replace(
            record,
            processing_status=normalized_status,
            processing_error=(
                str(processing_error)
                if processing_error is not None
                else (
                    record.processing_error
                    if normalized_status == DOCUMENT_PROCESSING_STATUS_FAILED
                    else ""
                )
            ),
            processing_started_at=(
                processing_started_at
                if processing_started_at is not None
                else (
                    record.processing_started_at
                    or now
                )
            ),
            processing_completed_at=(
                processing_completed_at
                if processing_completed_at is not None
                else (
                    now if normalized_status == DOCUMENT_PROCESSING_STATUS_READY else None
                )
            ),
            updated_at=now,
        )
        self._write_document_record(updated_record)
        return updated_record

    def delete_document(self, account_id: str, document_id: str) -> None:
        record = self.get_document(account_id, document_id)
        if record and record.original_storage_path:
            original_path = Path(record.original_storage_path)
            source_root = self._sources_dir(account_id)
            if original_path.exists():
                if original_path.is_dir():
                    shutil.rmtree(original_path, ignore_errors=True)
                else:
                    original_path.unlink(missing_ok=True)
            doc_dir = source_root / document_id
            if doc_dir.exists():
                shutil.rmtree(doc_dir, ignore_errors=True)

        self._record_path(account_id, document_id).unlink(missing_ok=True)

    def _write_document_record(self, record: KnowledgeDocumentRecord) -> None:
        record_path = self._record_path(record.account_id, record.document_id)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(record_path, record.to_dict())

    def _write_folder_record(self, record: KnowledgeFolderRecord) -> None:
        record_path = self._folder_record_path(record.account_id, record.folder_id)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write_json(record_path, record.to_dict())

    def _atomic_write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(path.parent),
            delete=False,
        ) as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            temp_name = fh.name

        Path(temp_name).replace(path)

    def _copy_source_if_local(
        self,
        account_id: str,
        document_id: str,
        source_path: str,
    ) -> Optional[str]:
        path = Path(source_path)
        if not path.exists():
            return None

        source_root = self._sources_dir(account_id) / document_id
        if source_root.exists():
            shutil.rmtree(source_root, ignore_errors=True)
        source_root.mkdir(parents=True, exist_ok=True)

        target = source_root / path.name
        if path.is_dir():
            shutil.copytree(path, target, dirs_exist_ok=True)
            return str(target)

        shutil.copy2(path, target)
        return str(target)


@lru_cache(maxsize=4)
def _cached_registry(workspace_path: str) -> KnowledgeDocumentRegistry:
    return KnowledgeDocumentRegistry(workspace_path)


def get_default_knowledge_document_registry() -> KnowledgeDocumentRegistry:
    from openviking_cli.utils.config.open_viking_config import get_openviking_config

    workspace_path = str(get_openviking_config().storage.workspace)
    return _cached_registry(workspace_path)


def update_default_document_processing_status(
    *,
    account_id: str,
    document_id: str,
    processing_status: str,
    processing_error: Optional[str] = None,
    processing_started_at: Optional[str] = None,
    processing_completed_at: Optional[str] = None,
) -> Optional[KnowledgeDocumentRecord]:
    registry = get_default_knowledge_document_registry()
    return registry.update_document_processing_status(
        account_id,
        document_id,
        processing_status=processing_status,
        processing_error=processing_error,
        processing_started_at=processing_started_at,
        processing_completed_at=processing_completed_at,
    )
