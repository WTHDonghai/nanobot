from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.service.knowledge_document_registry import KnowledgeDocumentRegistry
from openviking.service.resource_service import ResourceService
from openviking_cli.session.user_id import UserIdentifier


def _ctx() -> RequestContext:
    return RequestContext(user=UserIdentifier("acct", "tester", "agent"), role=Role.ROOT)


class _FakeVikingFS:
    def __init__(self, existing_uris: list[str]):
        self.existing_uris = set(existing_uris)
        self.mv_calls: list[tuple[str, str]] = []

    async def exists(self, uri: str, ctx: Optional[RequestContext] = None) -> bool:
        return uri in self.existing_uris

    async def mv(self, old_uri: str, new_uri: str, ctx: Optional[RequestContext] = None):
        self.mv_calls.append((old_uri, new_uri))
        self.existing_uris.discard(old_uri)
        self.existing_uris.add(new_uri)
        return {}

    def _get_vector_store(self):
        return None


@pytest.mark.anyio
async def test_move_document_updates_registry_and_resource_uri(tmp_path: Path):
    registry = KnowledgeDocumentRegistry(str(tmp_path / "workspace"))
    source = tmp_path / "guide.md"
    source.write_text("# guide\n", encoding="utf-8")

    current_folder = registry.create_folder(account_id="acct", name="当前目录")
    target_folder = registry.create_folder(account_id="acct", name="目标目录")
    document = registry.upsert_document(
        account_id="acct",
        source_path=str(source),
        resource_root_uri="viking://resources/当前目录/openviking/support-bot",
        folder_path=current_folder.path,
    )

    viking_fs = _FakeVikingFS(existing_uris=[document.resource_root_uri])
    service = ResourceService(
        viking_fs=viking_fs,
        resource_processor=object(),
        skill_processor=object(),
    )
    service._document_registry = registry

    result = await service.move_document(
        document_id=document.document_id,
        target_folder_path=target_folder.path,
        ctx=_ctx(),
    )

    assert viking_fs.mv_calls == [
        (
            "viking://resources/当前目录/openviking/support-bot",
            "viking://resources/目标目录/openviking/support-bot",
        )
    ]
    assert result["folder_path"] == "目标目录"
    assert result["resource_root_uri"] == "viking://resources/目标目录/openviking/support-bot"
    assert result["previous_folder_path"] == "当前目录"

    persisted = registry.get_document("acct", document.document_id)
    assert persisted is not None
    assert persisted.folder_path == "目标目录"
    assert persisted.resource_root_uri == "viking://resources/目标目录/openviking/support-bot"


@pytest.mark.anyio
async def test_rename_folder_moves_resource_subtree_and_updates_descendants(tmp_path: Path):
    registry = KnowledgeDocumentRegistry(str(tmp_path / "workspace"))
    source = tmp_path / "guide.md"
    source.write_text("# guide\n", encoding="utf-8")

    root_folder = registry.create_folder(account_id="acct", name="原目录")
    child_folder = registry.create_folder(account_id="acct", name="子目录", parent_path=root_folder.path)
    doc_a = registry.upsert_document(
        account_id="acct",
        source_path=str(source),
        resource_root_uri="viking://resources/原目录/总览",
        folder_path=root_folder.path,
    )
    doc_b = registry.upsert_document(
        account_id="acct",
        source_path=str(source),
        resource_root_uri="viking://resources/原目录/子目录/openviking/support-bot",
        folder_path=child_folder.path,
    )

    viking_fs = _FakeVikingFS(existing_uris=["viking://resources/原目录"])
    service = ResourceService(
        viking_fs=viking_fs,
        resource_processor=object(),
        skill_processor=object(),
    )
    service._document_registry = registry

    result = await service.rename_folder(
        folder_id=root_folder.folder_id,
        new_name="新目录",
        ctx=_ctx(),
    )

    assert viking_fs.mv_calls == [("viking://resources/原目录", "viking://resources/新目录")]
    assert result["path"] == "新目录"
    assert result["previous_path"] == "原目录"
    assert result["moved_document_count"] == 2
    assert result["moved_folder_count"] == 1

    folders = {folder.folder_id: folder for folder in registry.list_folders("acct")}
    assert folders[root_folder.folder_id].path == "新目录"
    assert folders[child_folder.folder_id].path == "新目录/子目录"

    updated_a = registry.get_document("acct", doc_a.document_id)
    updated_b = registry.get_document("acct", doc_b.document_id)
    assert updated_a is not None
    assert updated_b is not None
    assert updated_a.resource_root_uri == "viking://resources/新目录/总览"
    assert updated_b.resource_root_uri == "viking://resources/新目录/子目录/openviking/support-bot"
