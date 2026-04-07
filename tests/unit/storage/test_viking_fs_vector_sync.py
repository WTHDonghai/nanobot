from __future__ import annotations

from typing import Any, Dict, List

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.storage.expr import And, Eq
from openviking_cli.session.user_id import UserIdentifier


def _ctx() -> RequestContext:
    return RequestContext(user=UserIdentifier("acc", "user", "agent"), role=Role.ROOT)


def _extract_eq_value(filter_expr: Any, field: str) -> str:
    conds = filter_expr.conds if isinstance(filter_expr, And) else [filter_expr]
    for cond in conds:
        if isinstance(cond, Eq) and cond.field == field:
            return str(cond.value)
    raise AssertionError(f"Missing Eq({field!r}) in filter")


class _NoopLockContext:
    def __init__(self, *_args, **_kwargs):
        return None

    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


@pytest.mark.anyio
async def test_rewrite_uri_mappings_rolls_back_inserted_records_on_upsert_failure():
    from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend

    ctx = _ctx()
    backend = VikingVectorIndexBackend.__new__(VikingVectorIndexBackend)

    old_uri = "viking://resources/a"
    old_child_uri = "viking://resources/a/child"
    new_uri = "viking://resources/b"
    new_child_uri = "viking://resources/b/child"

    records_by_uri: Dict[str, List[Dict[str, Any]]] = {
        old_uri: [
            {
                "id": "old-a",
                "uri": old_uri,
                "level": 2,
                "account_id": ctx.account_id,
                "owner_space": "",
            }
        ],
        old_child_uri: [
            {
                "id": "old-child",
                "uri": old_child_uri,
                "level": 2,
                "account_id": ctx.account_id,
                "owner_space": "",
            }
        ],
    }
    deleted_ids: List[str] = []
    delete_uri_calls: List[List[str]] = []

    async def filter_impl(*, filter, limit=100, output_fields=None, ctx):
        uri = _extract_eq_value(filter, "uri")
        return [dict(record) for record in records_by_uri.get(uri, [])][:limit]

    async def upsert_impl(data, *, ctx):
        if data["uri"] == new_child_uri:
            raise RuntimeError("simulated upsert failure")
        return str(data["id"])

    async def delete_impl(ids, *, ctx):
        deleted_ids.extend(ids)
        return len(ids)

    async def delete_uris_impl(ctx, uris):
        delete_uri_calls.append(list(uris))

    backend.filter = filter_impl
    backend.upsert = upsert_impl
    backend.delete = delete_impl
    backend.delete_uris = delete_uris_impl

    with pytest.raises(RuntimeError, match="simulated upsert failure"):
        await backend.rewrite_uri_mappings(
            ctx=ctx,
            mappings=[(old_uri, new_uri), (old_child_uri, new_child_uri)],
        )

    assert len(deleted_ids) == 1
    assert delete_uri_calls == []


@pytest.mark.anyio
async def test_rewrite_uri_mappings_raises_if_old_uris_still_remain():
    from openviking.storage.viking_vector_index_backend import VikingVectorIndexBackend

    ctx = _ctx()
    backend = VikingVectorIndexBackend.__new__(VikingVectorIndexBackend)

    old_uri = "viking://resources/a"
    new_uri = "viking://resources/b"

    async def filter_impl(*, filter, limit=100, output_fields=None, ctx):
        uri = _extract_eq_value(filter, "uri")
        if uri == old_uri:
            return [
                {
                    "id": "old-a",
                    "uri": old_uri,
                    "level": 2,
                    "account_id": ctx.account_id,
                    "owner_space": "",
                }
            ]
        return []

    delete_uri_calls: List[List[str]] = []

    async def upsert_impl(data, *, ctx):
        return str(data["id"])

    async def delete_impl(ids, *, ctx):
        return len(ids)

    async def delete_uris_impl(ctx, uris):
        delete_uri_calls.append(list(uris))

    backend.filter = filter_impl
    backend.upsert = upsert_impl
    backend.delete = delete_impl
    backend.delete_uris = delete_uris_impl

    with pytest.raises(RuntimeError, match="Old vector URIs still remain after rewrite"):
        await backend.rewrite_uri_mappings(
            ctx=ctx,
            mappings=[(old_uri, new_uri)],
        )

    assert delete_uri_calls == [[old_uri], [old_uri], [old_uri]]


@pytest.mark.anyio
async def test_rm_aborts_when_vector_cleanup_fails(monkeypatch):
    from openviking.storage.viking_fs import VikingFS

    ctx = _ctx()

    class _FailingVectorStore:
        async def delete_uris(self, ctx, uris):
            raise RuntimeError("vectordb unavailable")

    class _FakeAGFS:
        def __init__(self):
            self.rm_calls: List[tuple[str, bool]] = []

        def stat(self, _path):
            return {"isDir": False}

        def rm(self, path, recursive: bool = False):
            self.rm_calls.append((path, recursive))
            return {}

    agfs = _FakeAGFS()

    class _FakeVikingFS(VikingFS):
        def __init__(self):
            super().__init__(agfs=agfs, vector_store=_FailingVectorStore())

        def _uri_to_path(self, uri, ctx=None):
            return f"/mock/{uri.replace('viking://', '')}"

        def _path_to_uri(self, path, ctx=None):
            if path.startswith("/mock/"):
                return f"viking://{path[len('/mock/') :]}"
            return super()._path_to_uri(path, ctx=ctx)

        async def _collect_uris(self, path, recursive, ctx=None):
            return ["viking://resources/doc/chunk-1"]

        def _ensure_access(self, uri, ctx):
            return None

    monkeypatch.setattr("openviking.storage.transaction.get_lock_manager", lambda: None)
    monkeypatch.setattr("openviking.storage.transaction.LockContext", _NoopLockContext)

    fs = _FakeVikingFS()

    with pytest.raises(RuntimeError, match="Failed to delete vector records"):
        await fs.rm("viking://resources/doc", ctx=ctx)

    assert agfs.rm_calls == []
