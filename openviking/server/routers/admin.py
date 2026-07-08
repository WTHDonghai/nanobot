# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Admin endpoints for OpenViking multi-tenant HTTP Server."""

from datetime import datetime, time, timezone, tzinfo
from typing import Any, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Body, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from openviking.server.auth import require_role
from openviking.server.dependencies import get_service
from openviking.server.identity import RequestContext, Role
from openviking.server.models import Response
from openviking.session.memory.utils.content import deserialize_content
from openviking.storage.viking_fs import get_viking_fs
from openviking_cli.exceptions import PermissionDeniedError
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])
USER_ID_PATTERN = r"^[A-Za-z0-9_-]+$"
MEMORY_DERIVED_FILENAMES = {".abstract.md", ".overview.md", ".relations.json"}
USER_MEMORY_CATEGORIES = {"profile", "preferences", "entities", "events"}
AGENT_MEMORY_CATEGORIES = {"cases", "patterns", "tools", "skills"}
ALL_MEMORY_CATEGORIES = USER_MEMORY_CATEGORIES | AGENT_MEMORY_CATEGORIES


class CreateAccountRequest(BaseModel):
    account_id: str
    admin_user_id: str


class RegisterUserRequest(BaseModel):
    user_id: str
    role: str = "user"


class SetRoleRequest(BaseModel):
    role: str


class AdminMemoryUpdateRequest(BaseModel):
    """Update an existing memory file through the admin surface."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., pattern=USER_ID_PATTERN)
    agent_id: str = Field("default", pattern=USER_ID_PATTERN)
    uri: str
    content: str
    wait: bool = True


class AdminMemoryDeleteRequest(BaseModel):
    """Delete an existing memory file through the admin surface."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(..., pattern=USER_ID_PATTERN)
    agent_id: str = Field("default", pattern=USER_ID_PATTERN)
    uri: str


def _get_api_key_manager(request: Request):
    """Get APIKeyManager from app state."""
    manager = getattr(request.app.state, "api_key_manager", None)
    if manager is None:
        raise PermissionDeniedError("Admin API requires root_api_key to be configured")
    return manager


def _check_account_access(ctx: RequestContext, account_id: str) -> None:
    """ADMIN can only operate on their own account."""
    if ctx.role == Role.ADMIN and ctx.account_id != account_id:
        raise PermissionDeniedError(f"ADMIN can only manage account: {ctx.account_id}")


def _target_memory_ctx(account_id: str, user_id: str, agent_id: str = "default") -> RequestContext:
    return RequestContext(
        user=UserIdentifier(account_id, user_id, agent_id or "default"),
        role=Role.ROOT,
    )


def _memory_scope_from_uri(uri: str) -> str:
    if uri.startswith("viking://user/"):
        return "user"
    if uri.startswith("viking://agent/"):
        return "agent"
    return ""


def _memory_category_from_uri(uri: str) -> str:
    marker = "/memories/"
    if marker not in uri:
        return ""
    relative = uri.split(marker, 1)[1].strip("/")
    if relative == "profile.md":
        return "profile"
    return relative.split("/", 1)[0]


def _is_memory_file_uri(uri: str) -> bool:
    name = uri.rstrip("/").rsplit("/", 1)[-1]
    return (
        "/memories/" in uri
        and uri.endswith(".md")
        and name not in MEMORY_DERIVED_FILENAMES
        and not name.startswith(".")
    )


def _allowed_memory_roots(target_ctx: RequestContext, scope: str) -> list[str]:
    roots: list[str] = []
    if scope in {"all", "user"}:
        roots.append(f"viking://user/{target_ctx.user.user_space_name()}/memories")
    if scope in {"all", "agent"}:
        roots.append(f"viking://agent/{target_ctx.user.agent_space_name()}/memories")
    return roots


def _ensure_memory_uri_allowed(uri: str, target_ctx: RequestContext) -> None:
    if not _is_memory_file_uri(uri):
        raise HTTPException(status_code=400, detail="URI must be a memory markdown file")
    roots = _allowed_memory_roots(target_ctx, "all")
    if not any(uri == root or uri.startswith(f"{root}/") for root in roots):
        raise PermissionDeniedError("Memory URI is outside the requested user/agent scope")


def _content_excerpt(content: str, max_len: int = 220) -> str:
    text = " ".join((content or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


def _parse_timezone(value: str) -> tzinfo:
    if not value:
        return timezone.utc
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(
            status_code=422,
            detail="Timezone must be a valid IANA timezone name",
        ) from exc


def _parse_day(
    value: Optional[str],
    *,
    end_of_day: bool = False,
    tz: tzinfo = timezone.utc,
) -> Optional[datetime]:
    if not value:
        return None
    has_time = "T" in value or " " in value
    try:
        day = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            day = datetime.combine(datetime.strptime(value, "%Y-%m-%d").date(), time.min)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="Date must be ISO datetime or YYYY-MM-DD",
            ) from exc
    if day.tzinfo is None:
        day = day.replace(tzinfo=tz)
    else:
        day = day.astimezone(tz)
    if end_of_day and not has_time:
        day = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif not has_time:
        day = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return day


# ---- Account endpoints ----


@router.post("/accounts")
async def create_account(
    body: CreateAccountRequest,
    request: Request,
    ctx: RequestContext = require_role(Role.ROOT),
):
    """Create a new account (workspace) with its first admin user."""
    manager = _get_api_key_manager(request)
    user_key = await manager.create_account(body.account_id, body.admin_user_id)
    service = get_service()
    account_ctx = RequestContext(
        user=UserIdentifier(body.account_id, body.admin_user_id, "default"),
        role=Role.ADMIN,
    )
    await service.initialize_account_directories(account_ctx)
    await service.initialize_user_directories(account_ctx)
    return Response(
        status="ok",
        result={
            "account_id": body.account_id,
            "admin_user_id": body.admin_user_id,
            "user_key": user_key,
        },
    )


@router.get("/accounts")
async def list_accounts(
    request: Request,
    ctx: RequestContext = require_role(Role.ROOT),
):
    """List all accounts."""
    manager = _get_api_key_manager(request)
    accounts = manager.get_accounts()
    return Response(status="ok", result=accounts)


@router.delete("/accounts/{account_id}")
async def delete_account(
    request: Request,
    account_id: str = Path(..., description="Account ID"),
    ctx: RequestContext = require_role(Role.ROOT),
):
    """Delete an account and cascade-clean its storage (AGFS + VectorDB)."""
    manager = _get_api_key_manager(request)

    # Build a ROOT-level context scoped to the target account for cleanup
    cleanup_ctx = RequestContext(
        user=UserIdentifier(account_id, "system", "system"),
        role=Role.ROOT,
    )

    # Cascade: remove AGFS data for the account
    viking_fs = get_viking_fs()
    account_prefixes = [
        "viking://user/",
        "viking://agent/",
        "viking://session/",
        "viking://resources/",
    ]
    for prefix in account_prefixes:
        try:
            await viking_fs.rm(prefix, recursive=True, ctx=cleanup_ctx)
        except Exception as e:
            logger.warning(f"AGFS cleanup for {prefix} in account {account_id}: {e}")

    # Cascade: remove VectorDB records for the account
    try:
        storage = viking_fs._get_vector_store()
        if storage:
            deleted = await storage.delete_account_data(account_id)
            logger.info(f"VectorDB cascade delete for account {account_id}: {deleted} records")
    except Exception as e:
        logger.warning(f"VectorDB cleanup for account {account_id}: {e}")

    # Finally delete the account metadata
    await manager.delete_account(account_id)
    return Response(status="ok", result={"deleted": True})


# ---- User endpoints ----


@router.post("/accounts/{account_id}/users")
async def register_user(
    body: RegisterUserRequest,
    request: Request,
    account_id: str = Path(..., description="Account ID"),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN),
):
    """Register a new user in an account."""
    _check_account_access(ctx, account_id)
    manager = _get_api_key_manager(request)
    user_key = await manager.register_user(account_id, body.user_id, body.role)
    service = get_service()
    user_ctx = RequestContext(
        user=UserIdentifier(account_id, body.user_id, "default"),
        role=Role.USER,
    )
    await service.initialize_user_directories(user_ctx)
    return Response(
        status="ok",
        result={
            "account_id": account_id,
            "user_id": body.user_id,
            "user_key": user_key,
        },
    )


@router.get("/accounts/{account_id}/users")
async def list_users(
    request: Request,
    account_id: str = Path(..., description="Account ID"),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN),
):
    """List all users in an account."""
    _check_account_access(ctx, account_id)
    manager = _get_api_key_manager(request)
    users = manager.get_users(account_id)
    return Response(status="ok", result=users)


@router.delete("/accounts/{account_id}/users/{user_id}")
async def remove_user(
    request: Request,
    account_id: str = Path(..., description="Account ID"),
    user_id: str = Path(..., description="User ID"),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN),
):
    """Remove a user from an account."""
    _check_account_access(ctx, account_id)
    manager = _get_api_key_manager(request)
    await manager.remove_user(account_id, user_id)
    return Response(status="ok", result={"deleted": True})


@router.put("/accounts/{account_id}/users/{user_id}/role")
async def set_user_role(
    body: SetRoleRequest,
    request: Request,
    account_id: str = Path(..., description="Account ID"),
    user_id: str = Path(..., description="User ID"),
    ctx: RequestContext = require_role(Role.ROOT),
):
    """Change a user's role (ROOT only)."""
    manager = _get_api_key_manager(request)
    await manager.set_role(account_id, user_id, body.role)
    return Response(
        status="ok",
        result={
            "account_id": account_id,
            "user_id": user_id,
            "role": body.role,
        },
    )


@router.post("/accounts/{account_id}/users/{user_id}/key")
async def regenerate_key(
    request: Request,
    account_id: str = Path(..., description="Account ID"),
    user_id: str = Path(..., description="User ID"),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN),
):
    """Regenerate a user's API key. Old key is immediately invalidated."""
    _check_account_access(ctx, account_id)
    manager = _get_api_key_manager(request)
    new_key = await manager.regenerate_key(account_id, user_id)
    return Response(status="ok", result={"user_key": new_key})


# ---- Memory admin endpoints ----


@router.get("/accounts/{account_id}/memories")
async def list_account_memories(
    request: Request,
    account_id: str = Path(..., description="Account ID"),
    user_id: Optional[str] = Query(None, pattern=USER_ID_PATTERN, description="Optional target user ID"),
    agent_id: str = Query("default", pattern=USER_ID_PATTERN, description="Target agent ID"),
    scope: Literal["all", "user", "agent"] = Query("all", description="Memory scope"),
    category: Optional[str] = Query(None, description="Memory category filter"),
    q: str = Query("", description="Search text in URI, category, or content"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN),
):
    """List memory files for one target user/agent namespace or every user in an account."""
    _check_account_access(ctx, account_id)
    normalized_category = (category or "").strip().lower()
    if normalized_category and normalized_category not in ALL_MEMORY_CATEGORIES:
        raise HTTPException(status_code=422, detail="Unknown memory category")

    service = get_service()
    normalized_user_id = (user_id or "").strip()
    normalized_agent_id = (agent_id or "default").strip() or "default"
    if normalized_user_id:
        target_user_ids = [normalized_user_id]
    else:
        manager = _get_api_key_manager(request)
        target_user_ids = [
            str(user.get("user_id") or "").strip()
            for user in manager.get_users(account_id)
            if str(user.get("user_id") or "").strip()
        ]
    query = q.strip().lower()
    items: list[dict[str, Any]] = []

    for target_user_id in target_user_ids:
        target_ctx = _target_memory_ctx(account_id, target_user_id, normalized_agent_id)
        for root in _allowed_memory_roots(target_ctx, scope):
            try:
                uris = await service.fs.ls(
                    root,
                    ctx=target_ctx,
                    recursive=True,
                    simple=True,
                    output="original",
                    node_limit=2000,
                    level_limit=20,
                )
            except Exception:
                continue

            for uri in uris:
                if not isinstance(uri, str) or not _is_memory_file_uri(uri):
                    continue
                item_category = _memory_category_from_uri(uri)
                if normalized_category and item_category != normalized_category:
                    continue

                try:
                    raw_content = await service.fs.read(uri, ctx=target_ctx)
                except Exception:
                    raw_content = ""
                content = deserialize_content(raw_content)
                searchable = (
                    f"{target_user_id}\n{normalized_agent_id}\n{uri}\n{item_category}\n{content}"
                ).lower()
                if query and query not in searchable:
                    continue

                try:
                    stat = await service.fs.stat(uri, ctx=target_ctx)
                except Exception:
                    stat = {}

                items.append(
                    {
                        "uri": uri,
                        "user_id": target_user_id,
                        "agent_id": normalized_agent_id,
                        "scope": _memory_scope_from_uri(uri),
                        "category": item_category,
                        "name": uri.rstrip("/").rsplit("/", 1)[-1],
                        "preview": _content_excerpt(content),
                        "content_length": len(content),
                        "updated_at": stat.get("modTime") or stat.get("updated_at") or "",
                        "size": stat.get("size", 0),
                    }
                )

    items.sort(
        key=lambda item: (
            str(item.get("updated_at") or ""),
            item["user_id"],
            item["agent_id"],
            item["uri"],
        ),
        reverse=True,
    )
    total = len(items)
    total_pages = (total + page_size - 1) // page_size if total else 0
    response_page = min(page, total_pages) if total_pages else 1
    start = (response_page - 1) * page_size
    end = start + page_size

    return Response(
        status="ok",
        result={
            "items": items[start:end],
            "total": total,
            "page": response_page,
            "page_size": page_size,
            "total_pages": total_pages,
            "scope": scope,
            "category": normalized_category,
            "user_id": normalized_user_id,
            "agent_id": normalized_agent_id,
            "target_user_ids": target_user_ids,
        },
    )


@router.get("/accounts/{account_id}/memories/detail")
async def get_account_memory_detail(
    account_id: str = Path(..., description="Account ID"),
    uri: str = Query(..., description="Memory URI"),
    user_id: str = Query(..., pattern=USER_ID_PATTERN, description="Target user ID"),
    agent_id: str = Query("default", pattern=USER_ID_PATTERN, description="Target agent ID"),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN),
):
    """Read one memory file."""
    _check_account_access(ctx, account_id)
    service = get_service()
    target_ctx = _target_memory_ctx(account_id, user_id, agent_id)
    _ensure_memory_uri_allowed(uri, target_ctx)

    raw_content = await service.fs.read(uri, ctx=target_ctx)
    content = deserialize_content(raw_content)
    try:
        stat = await service.fs.stat(uri, ctx=target_ctx)
    except Exception:
        stat = {}

    return Response(
        status="ok",
        result={
            "uri": uri,
            "scope": _memory_scope_from_uri(uri),
            "category": _memory_category_from_uri(uri),
            "name": uri.rstrip("/").rsplit("/", 1)[-1],
            "user_id": user_id,
            "agent_id": agent_id,
            "content": content,
            "content_length": len(content),
            "updated_at": stat.get("modTime") or stat.get("updated_at") or "",
            "size": stat.get("size", 0),
        },
    )


@router.put("/accounts/{account_id}/memories")
async def update_account_memory(
    body: AdminMemoryUpdateRequest = Body(...),
    account_id: str = Path(..., description="Account ID"),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN),
):
    """Replace one memory file and refresh its vector/semantic indexes."""
    _check_account_access(ctx, account_id)
    service = get_service()
    target_ctx = _target_memory_ctx(account_id, body.user_id, body.agent_id)
    _ensure_memory_uri_allowed(body.uri, target_ctx)

    write_result = await service.fs.write(
        uri=body.uri,
        content=body.content,
        ctx=target_ctx,
        mode="replace",
        wait=body.wait,
    )
    return Response(
        status="ok",
        result={
            "uri": body.uri,
            "write": write_result,
        },
    )


@router.delete("/accounts/{account_id}/memories")
async def delete_account_memory(
    body: AdminMemoryDeleteRequest = Body(...),
    account_id: str = Path(..., description="Account ID"),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN),
):
    """Delete one memory file and clean up matching vector records."""
    _check_account_access(ctx, account_id)
    service = get_service()
    target_ctx = _target_memory_ctx(account_id, body.user_id, body.agent_id)
    _ensure_memory_uri_allowed(body.uri, target_ctx)

    await service.fs.rm(body.uri, ctx=target_ctx, recursive=False)
    return Response(
        status="ok",
        result={
            "uri": body.uri,
            "deleted": True,
        },
    )


# ---- Analytics and session audit endpoints ----


@router.get("/accounts/{account_id}/analytics/daily")
async def get_daily_analytics(
    account_id: str = Path(..., description="Account ID"),
    user_id: Optional[str] = Query(
        None,
        pattern=USER_ID_PATTERN,
        description="Optional user ID filter",
    ),
    from_date: Optional[str] = Query(None, description="Start date, YYYY-MM-DD"),
    to_date: Optional[str] = Query(None, description="End date, YYYY-MM-DD"),
    tz: str = Query("UTC", description="IANA timezone for date filters and buckets"),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN),
):
    """Get daily usage statistics for an account."""
    _check_account_access(ctx, account_id)
    service = get_service()
    timezone_info = _parse_timezone(tz)
    result = await service.sessions.get_admin_daily_analytics(
        account_id,
        user_id=user_id or "",
        from_date=_parse_day(from_date, tz=timezone_info),
        to_date=_parse_day(to_date, end_of_day=True, tz=timezone_info),
        tz=timezone_info,
    )
    return Response(status="ok", result=result)


@router.get("/accounts/{account_id}/sessions")
async def list_account_sessions(
    account_id: str = Path(..., description="Account ID"),
    user_id: Optional[str] = Query(
        None,
        pattern=USER_ID_PATTERN,
        description="Optional user ID filter",
    ),
    from_date: Optional[str] = Query(None, description="Start date, YYYY-MM-DD"),
    to_date: Optional[str] = Query(None, description="End date, YYYY-MM-DD"),
    tz: str = Query("UTC", description="IANA timezone for date filters"),
    q: str = Query("", description="Search text in raw messages and tool records"),
    sort_by: str = Query("last_active", description="Sort field"),
    sort_order: str = Query("desc", pattern="^(asc|desc)$", description="Sort order"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=1000),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN),
):
    """List sessions across users in an account for admin audit."""
    _check_account_access(ctx, account_id)
    service = get_service()
    timezone_info = _parse_timezone(tz)
    result = await service.sessions.list_admin_sessions_paginated(
        account_id,
        user_id=user_id or "",
        from_date=_parse_day(from_date, tz=timezone_info),
        to_date=_parse_day(to_date, end_of_day=True, tz=timezone_info),
        query=q.strip(),
        sort_by=sort_by.strip(),
        sort_order=sort_order.strip(),
        page=page,
        page_size=page_size,
    )
    return Response(status="ok", result=result)


@router.get("/accounts/{account_id}/sessions/{session_id}")
async def get_account_session_detail(
    account_id: str = Path(..., description="Account ID"),
    session_id: str = Path(..., description="Session ID"),
    user_id: str = Query(
        ...,
        pattern=USER_ID_PATTERN,
        description="User ID that owns the session",
    ),
    include_messages: bool = Query(True, description="Include raw messages"),
    q: str = Query("", description="Search text in raw messages and tool records"),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN),
):
    """Get raw session audit detail across users in an account."""
    _check_account_access(ctx, account_id)
    service = get_service()
    result = await service.sessions.get_admin_session_detail(
        account_id,
        user_id,
        session_id,
        include_messages=include_messages,
        query=q.strip(),
    )
    return Response(status="ok", result=result)


@router.delete("/accounts/{account_id}/sessions/{session_id}")
async def delete_account_session(
    account_id: str = Path(..., description="Account ID"),
    session_id: str = Path(..., description="Session ID"),
    user_id: str = Query(
        ...,
        pattern=USER_ID_PATTERN,
        description="User ID that owns the session",
    ),
    ctx: RequestContext = require_role(Role.ROOT, Role.ADMIN),
):
    """Delete a target user's session from an admin audit view."""
    _check_account_access(ctx, account_id)
    service = get_service()
    await service.sessions.delete_admin_session(account_id, user_id, session_id)
    return Response(
        status="ok",
        result={
            "account_id": account_id,
            "user_id": user_id,
            "session_id": session_id,
        },
    )
