from types import SimpleNamespace

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.service.knowledge_document_registry import KnowledgeDocumentRegistry
from openviking.service.resource_service import ResourceService
from openviking_cli.session.user_id import UserIdentifier


class _DummyResourceProcessor:
    def __init__(self, *, processing_requested: bool = True, processing_enqueued: bool = True):
        self.calls = []
        self.processing_requested = processing_requested
        self.processing_enqueued = processing_enqueued

    async def process_resource(self, **kwargs):
        self.calls.append(kwargs)
        result = {
            "status": "success",
            "root_uri": kwargs.get("to") or "viking://resources/fallback",
            "source_format": "markdown",
            "meta": {},
            "processing_requested": self.processing_requested,
            "processing_enqueued": self.processing_enqueued,
        }
        post_finalize_hook = kwargs.get("post_finalize_hook")
        if post_finalize_hook:
            document = await post_finalize_hook(result)
            if document:
                result["document_id"] = document["document_id"]
                result["knowledge_document"] = document
        if self.processing_requested and not self.processing_enqueued:
            result["processing_error"] = "enqueue failed"
        return result


@pytest.mark.anyio
async def test_add_resource_derives_target_uri_from_virtual_folder_path():
    processor = _DummyResourceProcessor()
    service = ResourceService(
        viking_fs=SimpleNamespace(),
        resource_processor=processor,
        skill_processor=SimpleNamespace(),
    )
    ctx = RequestContext(user=UserIdentifier.the_default_user("tester"), role=Role.ROOT)

    result = await service.add_resource(
        path="/tmp/upload_123/03-reception.docx",
        ctx=ctx,
        folder_path="a/b/c",
        source_ref="03-reception.docx",
    )

    assert processor.calls[0]["to"] == "viking://resources/a/b/c/03-reception"
    assert result["root_uri"] == "viking://resources/a/b/c/03-reception"


@pytest.mark.anyio
async def test_add_resource_keeps_explicit_target_uri():
    processor = _DummyResourceProcessor()
    service = ResourceService(
        viking_fs=SimpleNamespace(),
        resource_processor=processor,
        skill_processor=SimpleNamespace(),
    )
    ctx = RequestContext(user=UserIdentifier.the_default_user("tester"), role=Role.ROOT)

    await service.add_resource(
        path="/tmp/upload_123/03-reception.docx",
        ctx=ctx,
        folder_path="a/b/c",
        source_ref="03-reception.docx",
        to="viking://resources/custom/manual",
    )

    assert processor.calls[0]["to"] == "viking://resources/custom/manual"


@pytest.mark.anyio
async def test_add_resource_registers_processing_status_when_async_work_is_pending(tmp_path):
    processor = _DummyResourceProcessor(processing_requested=True, processing_enqueued=True)
    service = ResourceService(
        viking_fs=SimpleNamespace(),
        resource_processor=processor,
        skill_processor=SimpleNamespace(),
    )
    service._document_registry = KnowledgeDocumentRegistry(str(tmp_path / "workspace"))
    ctx = RequestContext(user=UserIdentifier.the_default_user("tester"), role=Role.ROOT)

    source = tmp_path / "pending.md"
    source.write_text("# pending\n", encoding="utf-8")

    result = await service.add_resource(
        path=str(source),
        ctx=ctx,
        source_ref="pending.md",
    )

    persisted = service._document_registry.get_document(ctx.account_id, result["document_id"])
    assert persisted is not None
    assert persisted.processing_status == "processing"
    assert persisted.processing_completed_at is None


@pytest.mark.anyio
async def test_add_resource_marks_document_failed_when_enqueue_fails(tmp_path):
    processor = _DummyResourceProcessor(processing_requested=True, processing_enqueued=False)
    service = ResourceService(
        viking_fs=SimpleNamespace(),
        resource_processor=processor,
        skill_processor=SimpleNamespace(),
    )
    service._document_registry = KnowledgeDocumentRegistry(str(tmp_path / "workspace"))
    ctx = RequestContext(user=UserIdentifier.the_default_user("tester"), role=Role.ROOT)

    source = tmp_path / "failed.md"
    source.write_text("# failed\n", encoding="utf-8")

    result = await service.add_resource(
        path=str(source),
        ctx=ctx,
        source_ref="failed.md",
    )

    persisted = service._document_registry.get_document(ctx.account_id, result["document_id"])
    assert persisted is not None
    assert persisted.processing_status == "failed"
    assert persisted.processing_error == "enqueue failed"
