from types import SimpleNamespace

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.service.resource_service import ResourceService
from openviking_cli.session.user_id import UserIdentifier


class _DummyResourceProcessor:
    def __init__(self):
        self.calls = []

    async def process_resource(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "status": "success",
            "root_uri": kwargs.get("to") or "viking://resources/fallback",
            "meta": {},
        }


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
