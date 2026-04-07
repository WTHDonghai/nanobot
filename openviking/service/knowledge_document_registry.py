"""Persistent registry for user-managed knowledge documents and virtual folders."""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from openviking_cli.exceptions import ConflictError, InvalidArgumentError, NotFoundError


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
    original_storage_path: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "KnowledgeDocumentRecord":
        return cls(
            document_id=str(data["document_id"]),
            account_id=str(data["account_id"]),
            display_name=str(data.get("display_name") or ""),
            source_type=str(data.get("source_type") or "unknown"),
            source_ref=str(data.get("source_ref") or ""),
            resource_root_uri=str(data.get("resource_root_uri") or ""),
            created_at=str(data.get("created_at") or _utc_now_iso()),
            updated_at=str(data.get("updated_at") or _utc_now_iso()),
            folder_path=_normalize_folder_path(data.get("folder_path")),
            source_format=data.get("source_format"),
            reason=str(data.get("reason") or ""),
            instruction=str(data.get("instruction") or ""),
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
            "has_local_copy": bool(self.original_storage_path),
        }


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
            original_storage_path=original_storage_path,
            meta=_json_safe(meta or {}),
        )
        self._write_document_record(record)
        return record

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
