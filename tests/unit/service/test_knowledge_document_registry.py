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
