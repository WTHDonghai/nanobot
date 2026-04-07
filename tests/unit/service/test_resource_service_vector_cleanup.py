from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.service.resource_service import ResourceService
from openviking.storage.expr import PathScope
from openviking_cli.session.user_id import UserIdentifier


def _ctx() -> RequestContext:
    return RequestContext(user=UserIdentifier("acc", "user", "agent"), role=Role.ROOT)


class _FakeVectorStore:
    def __init__(self, records: List[Dict[str, Any]]):
        self.records = list(records)
        self.delete_calls: List[List[str]] = []

    async def scroll(
        self,
        *,
        filter=None,
        limit: int = 100,
        cursor: Optional[str] = None,
        output_fields: Optional[List[str]] = None,
        ctx: RequestContext,
    ):
        filtered = list(self.records)
        if isinstance(filter, PathScope):
            prefix = filter.path.rstrip("/")
            filtered = [
                record
                for record in filtered
                if str(record.get("uri") or "") == prefix
                or str(record.get("uri") or "").startswith(f"{prefix}/")
            ]

        offset = int(cursor) if cursor else 0
        page = filtered[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(filtered) else None

        if output_fields is None:
            projected = [dict(record) for record in page]
        else:
            projected = [
                {field: record.get(field) for field in output_fields if field in record}
                for record in page
            ]
        return projected, next_cursor

    async def delete_uris(self, ctx: RequestContext, uris: List[str]) -> None:
        self.delete_calls.append(list(uris))


class _FakeVikingFS:
    def __init__(self, *, vector_store: Optional[_FakeVectorStore], existing_uris: List[str]):
        self._vector_store = vector_store
        self._existing_uris = set(existing_uris)
        self.exists_calls: List[str] = []

    def _get_vector_store(self):
        return self._vector_store

    async def exists(self, uri: str, ctx: Optional[RequestContext] = None) -> bool:
        self.exists_calls.append(uri)
        return uri in self._existing_uris


@pytest.mark.anyio
async def test_cleanup_orphan_resource_vectors_dry_run_reports_missing_resource_uris():
    ctx = _ctx()
    vector_store = _FakeVectorStore(
        [
            {"uri": "viking://resources/a"},
            {"uri": "viking://resources/missing"},
            {"uri": "viking://resources/missing"},
            {"uri": "viking://session/thread/1"},
        ]
    )
    viking_fs = _FakeVikingFS(
        vector_store=vector_store,
        existing_uris=["viking://resources/a"],
    )
    service = ResourceService(
        viking_fs=viking_fs,
        resource_processor=object(),
        skill_processor=object(),
    )

    result = await service.cleanup_orphan_resource_vectors(
        ctx=ctx,
        dry_run=True,
        batch_size=2,
        preview_limit=10,
    )

    assert result == {
        "dry_run": True,
        "checked_vector_record_count": 3,
        "checked_resource_uri_count": 2,
        "orphan_uri_count": 1,
        "orphan_vector_record_count": 2,
        "deleted_uri_count": 0,
        "deleted_vector_record_count": 0,
        "orphan_uris": ["viking://resources/missing"],
        "orphan_uris_truncated": False,
    }
    assert vector_store.delete_calls == []
    assert viking_fs.exists_calls == ["viking://resources/a", "viking://resources/missing"]


@pytest.mark.anyio
async def test_cleanup_orphan_resource_vectors_deletes_orphans():
    ctx = _ctx()
    vector_store = _FakeVectorStore(
        [
            {"uri": "viking://resources/a"},
            {"uri": "viking://resources/missing-a"},
            {"uri": "viking://resources/missing-b"},
            {"uri": "viking://resources/missing-b"},
        ]
    )
    viking_fs = _FakeVikingFS(
        vector_store=vector_store,
        existing_uris=["viking://resources/a"],
    )
    service = ResourceService(
        viking_fs=viking_fs,
        resource_processor=object(),
        skill_processor=object(),
    )

    result = await service.cleanup_orphan_resource_vectors(
        ctx=ctx,
        dry_run=False,
        batch_size=10,
        preview_limit=1,
    )

    assert result == {
        "dry_run": False,
        "checked_vector_record_count": 4,
        "checked_resource_uri_count": 3,
        "orphan_uri_count": 2,
        "orphan_vector_record_count": 3,
        "deleted_uri_count": 2,
        "deleted_vector_record_count": 3,
        "orphan_uris": ["viking://resources/missing-a"],
        "orphan_uris_truncated": True,
    }
    assert vector_store.delete_calls == [
        ["viking://resources/missing-a", "viking://resources/missing-b"]
    ]


@pytest.mark.anyio
async def test_cleanup_orphan_resource_vectors_handles_missing_vector_store():
    ctx = _ctx()
    service = ResourceService(
        viking_fs=_FakeVikingFS(vector_store=None, existing_uris=[]),
        resource_processor=object(),
        skill_processor=object(),
    )

    result = await service.cleanup_orphan_resource_vectors(ctx=ctx)

    assert result["checked_vector_record_count"] == 0
    assert result["orphan_uri_count"] == 0
    assert result["deleted_uri_count"] == 0
