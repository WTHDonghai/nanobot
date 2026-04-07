from pathlib import Path

import pytest

from openviking.service.knowledge_document_registry import KnowledgeDocumentRegistry
from openviking_cli.exceptions import ConflictError


def test_registry_persists_source_copy_and_deletes_it(tmp_path: Path):
    workspace = tmp_path / "workspace"
    source = tmp_path / "guide.md"
    source.write_text("# hello\n", encoding="utf-8")

    registry = KnowledgeDocumentRegistry(str(workspace))
    product_folder = registry.create_folder(account_id="acct", name="产品资料")
    release_folder = registry.create_folder(
        account_id="acct",
        name="发版说明",
        parent_path=product_folder.path,
    )

    record = registry.upsert_document(
        account_id="acct",
        source_path=str(source),
        resource_root_uri="viking://resources/guide",
        folder_path=release_folder.path,
        source_format="markdown",
        reason="import test",
        instruction="keep structure",
        meta={"pages": 1},
    )

    assert record.display_name == "guide.md"
    assert record.document_id
    assert record.folder_path == "产品资料/发版说明"
    assert record.original_storage_path is not None

    copied_path = Path(record.original_storage_path)
    assert copied_path.exists()
    assert copied_path.read_text(encoding="utf-8") == "# hello\n"

    listed = registry.list_documents("acct")
    assert len(listed) == 1
    assert listed[0].document_id == record.document_id
    assert listed[0].resource_root_uri == "viking://resources/guide"
    assert listed[0].folder_path == release_folder.path

    folders = registry.list_folders("acct")
    assert [folder.path for folder in folders] == ["产品资料", "产品资料/发版说明"]

    updated_source = tmp_path / "guide-v2.md"
    updated_source.write_text("# hello v2\n", encoding="utf-8")
    updated = registry.upsert_document(
        account_id="acct",
        source_path=str(updated_source),
        resource_root_uri="viking://resources/guide",
        source_format="markdown",
    )

    assert updated.document_id == record.document_id
    assert updated.display_name == "guide-v2.md"
    assert updated.folder_path == release_folder.path

    with pytest.raises(ConflictError):
        registry.delete_folder("acct", product_folder.folder_id)

    registry.delete_document("acct", record.document_id)
    registry.delete_folder("acct", release_folder.folder_id)
    registry.delete_folder("acct", product_folder.folder_id)

    assert registry.list_documents("acct") == []
    assert registry.list_folders("acct") == []
    assert not copied_path.exists()


def test_registry_moves_document_and_preserves_relative_resource_suffix(tmp_path: Path):
    registry = KnowledgeDocumentRegistry(str(tmp_path / "workspace"))
    archive_folder = registry.create_folder(account_id="acct", name="归档")

    source = tmp_path / "repo.txt"
    source.write_text("repo", encoding="utf-8")
    record = registry.upsert_document(
        account_id="acct",
        source_path=str(source),
        source_ref="git@github.com:openviking/support-bot.git",
        resource_root_uri="viking://resources/openviking/support-bot",
    )

    plan = registry.plan_move_document(
        account_id="acct",
        document_id=record.document_id,
        target_folder_path=archive_folder.path,
    )
    moved = registry.apply_document_move(plan=plan)

    assert moved.folder_path == "归档"
    assert moved.resource_root_uri == "viking://resources/归档/openviking/support-bot"

    persisted = registry.get_document("acct", record.document_id)
    assert persisted is not None
    assert persisted.folder_path == "归档"
    assert persisted.resource_root_uri == "viking://resources/归档/openviking/support-bot"


def test_registry_renames_folder_and_updates_descendants(tmp_path: Path):
    registry = KnowledgeDocumentRegistry(str(tmp_path / "workspace"))
    source = tmp_path / "guide.md"
    source.write_text("# guide\n", encoding="utf-8")

    top_folder = registry.create_folder(account_id="acct", name="产品资料")
    child_folder = registry.create_folder(
        account_id="acct",
        name="接口文档",
        parent_path=top_folder.path,
    )

    doc_a = registry.upsert_document(
        account_id="acct",
        source_path=str(source),
        resource_root_uri="viking://resources/产品资料/总览",
        folder_path=top_folder.path,
    )
    doc_b = registry.upsert_document(
        account_id="acct",
        source_path=str(source),
        resource_root_uri="viking://resources/产品资料/接口文档/openviking/support-bot",
        folder_path=child_folder.path,
    )

    plan = registry.plan_rename_folder(
        account_id="acct",
        folder_id=top_folder.folder_id,
        new_name="平台资料",
    )
    renamed = registry.apply_folder_rename(plan=plan)

    assert renamed.path == "平台资料"
    assert [folder.path for folder in registry.list_folders("acct")] == ["平台资料", "平台资料/接口文档"]

    updated_a = registry.get_document("acct", doc_a.document_id)
    updated_b = registry.get_document("acct", doc_b.document_id)
    assert updated_a is not None
    assert updated_b is not None
    assert updated_a.folder_path == "平台资料"
    assert updated_a.resource_root_uri == "viking://resources/平台资料/总览"
    assert updated_b.folder_path == "平台资料/接口文档"
    assert updated_b.resource_root_uri == "viking://resources/平台资料/接口文档/openviking/support-bot"
