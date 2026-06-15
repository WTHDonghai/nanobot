# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Session Service for OpenViking.

Provides session management operations: session, sessions, add_message, commit, delete.
"""

import json
from datetime import datetime, time, timedelta, timezone, tzinfo
from functools import cmp_to_key
from typing import Any, Dict, List, Optional

from openviking.message import Message
from openviking.message.part import ContextPart, TextPart, ToolPart
from openviking.server.identity import RequestContext, Role
from openviking.service.task_tracker import get_task_tracker
from openviking.session import AUDIT_SUMMARY_VERSION, Session, SessionMeta
from openviking.session.compressor import SessionCompressor
from openviking.session.memory_scope import ALL_MEMORY_SCOPE, normalize_memory_scope
from openviking.storage import VikingDBManager
from openviking.storage.viking_fs import VikingFS
from openviking_cli.exceptions import NotFoundError, NotInitializedError
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils import get_logger

logger = get_logger(__name__)
FAILED_TOOL_STATUSES = {"error", "failed"}
DEFAULT_ADMIN_SESSION_SORT_BY = "last_active"
DEFAULT_ADMIN_SESSION_SORT_ORDER = "desc"
MAX_ADMIN_DAILY_BUCKETS = 120
ADMIN_SESSION_SORT_FIELDS = {
    "last_active",
    "created_at",
    "updated_at",
    "message_count",
    "user_message_count",
    "assistant_message_count",
    "tool_call_count",
    "failed_tool_call_count",
    "token_total",
    "matched_message_count",
    "user_id",
}


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _date_key(dt: Optional[datetime], tz: tzinfo = timezone.utc) -> str:
    if dt is None:
        return "unknown"
    return dt.astimezone(tz).date().isoformat()


def _empty_daily_analytics_row(day: str) -> Dict[str, Any]:
    return {
        "date": day,
        "active_users": set(),
        "session_count": 0,
        "message_count": 0,
        "user_message_count": 0,
        "assistant_message_count": 0,
        "tool_call_count": 0,
        "failed_tool_call_count": 0,
        "token_usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def _session_activity_value(item: Dict[str, Any]) -> str:
    return (
        item.get("last_message_at")
        or item.get("updated_at")
        or item.get("created_at")
        or ""
    )


def _admin_session_sort_key(item: Dict[str, Any]) -> tuple:
    activity_dt = _parse_datetime(_session_activity_value(item))
    activity_ts = activity_dt.timestamp() if activity_dt else 0
    return (-activity_ts, item.get("user_id", ""), item.get("session_id", ""))


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _token_total(item: Dict[str, Any]) -> int:
    token_usage = item.get("token_usage") or {}
    if not isinstance(token_usage, dict):
        return 0
    return _safe_int(token_usage.get("total_tokens"))


def _timestamp(value: Any) -> float:
    parsed = _parse_datetime(value)
    return parsed.timestamp() if parsed else 0


def _admin_session_sort_value(item: Dict[str, Any], sort_by: str) -> Any:
    if sort_by == "last_active":
        return _timestamp(_session_activity_value(item))
    if sort_by in {"created_at", "updated_at"}:
        return _timestamp(item.get(sort_by))
    if sort_by == "token_total":
        return _token_total(item)
    if sort_by == "user_id":
        return str(item.get("user_id", "")).lower()
    return _safe_int(item.get(sort_by))


def _compare_values(left: Any, right: Any) -> int:
    return (left > right) - (left < right)


def _sort_admin_sessions(
    sessions: List[Dict[str, Any]],
    *,
    sort_by: str = DEFAULT_ADMIN_SESSION_SORT_BY,
    sort_order: str = DEFAULT_ADMIN_SESSION_SORT_ORDER,
) -> None:
    normalized_sort_by = (
        sort_by if sort_by in ADMIN_SESSION_SORT_FIELDS else DEFAULT_ADMIN_SESSION_SORT_BY
    )
    normalized_sort_order = (
        sort_order.lower() if sort_order.lower() in {"asc", "desc"} else DEFAULT_ADMIN_SESSION_SORT_ORDER
    )

    def compare(left: Dict[str, Any], right: Dict[str, Any]) -> int:
        primary = _compare_values(
            _admin_session_sort_value(left, normalized_sort_by),
            _admin_session_sort_value(right, normalized_sort_by),
        )
        if primary:
            return -primary if normalized_sort_order == "desc" else primary

        if normalized_sort_by != "last_active":
            activity = _compare_values(
                _admin_session_sort_value(left, "last_active"),
                _admin_session_sort_value(right, "last_active"),
            )
            if activity:
                return -activity

        user = _compare_values(str(left.get("user_id", "")), str(right.get("user_id", "")))
        if user:
            return user
        return _compare_values(
            str(left.get("session_id", "")),
            str(right.get("session_id", "")),
        )

    sessions.sort(key=cmp_to_key(compare))


def _message_token_usage(message: Message) -> Dict[str, int]:
    usage = message.token_usage or {}
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", 0) or 0)

    for part in message.parts:
        if isinstance(part, ToolPart):
            prompt_tokens += int(part.prompt_tokens or 0)
            completion_tokens += int(part.completion_tokens or 0)

    merged_total = prompt_tokens + completion_tokens
    if merged_total > 0:
        total_tokens = max(total_tokens, merged_total)
    if total_tokens <= 0:
        total_tokens = int(message.estimated_tokens or 0)

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
    }


def _merge_token_usage(total: Dict[str, int], usage: Dict[str, int]) -> None:
    total["prompt_tokens"] += int(usage.get("prompt_tokens", 0) or 0)
    total["completion_tokens"] += int(usage.get("completion_tokens", 0) or 0)
    total["total_tokens"] += int(usage.get("total_tokens", 0) or 0)


def _is_failed_tool_status(status: str) -> bool:
    return status in FAILED_TOOL_STATUSES


def _stringify_search_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _message_matches_query(message: Message, query: str) -> bool:
    if not query:
        return True
    needle = query.lower()
    haystacks: List[str] = [message.role, message.id]
    for part in message.parts:
        if isinstance(part, TextPart):
            haystacks.append(part.text)
        elif isinstance(part, ToolPart):
            haystacks.extend(
                [
                    part.tool_id,
                    part.tool_name,
                    part.tool_uri,
                    part.skill_uri,
                    part.tool_status,
                    _stringify_search_value(part.tool_input),
                    part.tool_output or "",
                ]
            )
        elif isinstance(part, ContextPart):
            haystacks.extend([part.uri, part.context_type, part.abstract])
    return any(needle in value.lower() for value in haystacks if value)


class SessionService:
    """Session management service."""

    def __init__(
        self,
        vikingdb: Optional[VikingDBManager] = None,
        viking_fs: Optional[VikingFS] = None,
        session_compressor: Optional[SessionCompressor] = None,
    ):
        self._vikingdb = vikingdb
        self._viking_fs = viking_fs
        self._session_compressor = session_compressor

    def set_dependencies(
        self,
        vikingdb: VikingDBManager,
        viking_fs: VikingFS,
        session_compressor: SessionCompressor,
    ) -> None:
        """Set dependencies (for deferred initialization)."""
        self._vikingdb = vikingdb
        self._viking_fs = viking_fs
        self._session_compressor = session_compressor

    def _ensure_initialized(self) -> None:
        """Ensure all dependencies are initialized."""
        if not self._viking_fs:
            raise NotInitializedError("VikingFS")

    def session(self, ctx: RequestContext, session_id: Optional[str] = None) -> Session:
        """Create a new session or load an existing one.

        Args:
            session_id: Session ID, creates a new session (auto-generated ID) if None

        Returns:
            Session instance
        """
        self._ensure_initialized()
        return Session(
            viking_fs=self._viking_fs,
            vikingdb_manager=self._vikingdb,
            session_compressor=self._session_compressor,
            user=ctx.user,
            ctx=ctx,
            session_id=session_id,
        )

    async def create(self, ctx: RequestContext) -> Session:
        """Create a session and persist its root path."""
        session = self.session(ctx)
        await session.ensure_exists()
        return session

    async def get(
        self, session_id: str, ctx: RequestContext, *, auto_create: bool = False
    ) -> Session:
        """Get an existing session.

        Args:
            session_id: Session ID
            ctx: Request context
            auto_create: If True, create the session when it does not exist.
                         Default is False (raise NotFoundError).
        """
        session = self.session(ctx, session_id)
        if not await session.exists():
            if not auto_create:
                raise NotFoundError(session_id, "session")
            await session.ensure_exists()
        await session.load()
        return session

    async def sessions(self, ctx: RequestContext) -> List[Dict[str, Any]]:
        """Get all sessions for the current user.

        Returns:
            List of session info dicts
        """
        self._ensure_initialized()
        session_base_uri = f"viking://session/{ctx.user.user_space_name()}"

        try:
            entries = await self._viking_fs.ls(session_base_uri, ctx=ctx)
            sessions = []
            for entry in entries:
                name = entry.get("name", "")
                if name in [".", ".."]:
                    continue
                sessions.append(
                    {
                        "session_id": name,
                        "uri": f"{session_base_uri}/{name}",
                        "is_dir": entry.get("isDir", False),
                    }
                )
            return sessions
        except Exception:
            return []

    def _user_ctx(
        self,
        account_id: str,
        user_id: str,
        *,
        agent_id: str = "default",
        role: Role = Role.ROOT,
    ) -> RequestContext:
        return RequestContext(
            user=UserIdentifier(account_id, user_id, agent_id),
            role=role,
        )

    def _summarize_messages(self, messages: List[Message]) -> Dict[str, Any]:
        token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        tool_calls = 0
        failed_tool_calls = 0
        context_refs = 0
        first_at: Optional[datetime] = None
        last_at: Optional[datetime] = None

        for message in messages:
            created_at = _parse_datetime(message.created_at)
            if created_at:
                if first_at is None or created_at < first_at:
                    first_at = created_at
                if last_at is None or created_at > last_at:
                    last_at = created_at

            _merge_token_usage(token_usage, _message_token_usage(message))
            for part in message.parts:
                if isinstance(part, ToolPart):
                    tool_calls += 1
                    if _is_failed_tool_status(part.tool_status):
                        failed_tool_calls += 1
                elif isinstance(part, ContextPart):
                    context_refs += 1

        return {
            "message_count": len(messages),
            "user_message_count": sum(1 for message in messages if message.role == "user"),
            "assistant_message_count": sum(
                1 for message in messages if message.role == "assistant"
            ),
            "tool_call_count": tool_calls,
            "failed_tool_call_count": failed_tool_calls,
            "context_ref_count": context_refs,
            "token_usage": token_usage,
            "first_message_at": first_at.isoformat() if first_at else "",
            "last_message_at": last_at.isoformat() if last_at else "",
        }

    async def _read_all_session_messages(self, session: Session) -> List[Message]:
        """Return raw messages from completed archives, pending archives, and live JSONL."""
        messages: List[Message] = []
        archive_refs = await self._list_session_archive_refs(session)
        for archive in archive_refs:
            messages.extend(await session._read_archive_messages(archive["archive_uri"]))
        messages.extend(session.messages)
        return messages

    async def _list_session_archive_refs(self, session: Session) -> List[Dict[str, Any]]:
        """List archive refs without relying on potentially stale meta commit_count."""
        try:
            history_items = await self._viking_fs.ls(f"{session.uri}/history", ctx=session.ctx)
        except Exception:
            return []

        refs: List[Dict[str, Any]] = []
        for item in history_items:
            name = item.get("name") if isinstance(item, dict) else str(item)
            if not name or not name.startswith("archive_"):
                continue
            try:
                index = int(name.split("_")[1])
            except Exception:
                continue
            refs.append(
                {
                    "archive_id": name,
                    "archive_uri": f"{session.uri}/history/{name}",
                    "index": index,
                }
            )
        return sorted(refs, key=lambda item: item["index"])

    async def get_admin_session_detail(
        self,
        account_id: str,
        user_id: str,
        session_id: str,
        *,
        include_messages: bool = True,
        query: str = "",
    ) -> Dict[str, Any]:
        """Get a cross-user session audit detail for admin endpoints."""
        target_ctx = self._user_ctx(account_id, user_id)
        session = await self.get(session_id, target_ctx)
        messages = await self._read_all_session_messages(session)
        matched_message_count = (
            sum(1 for message in messages if _message_matches_query(message, query))
            if query
            else len(messages)
        )

        result = {
            **session.meta.to_dict(),
            "account_id": account_id,
            "user_id": user_id,
            "session_id": session_id,
            "uri": session.uri,
            "matched_message_count": matched_message_count,
            **self._summarize_messages(messages),
        }
        if include_messages:
            result["messages"] = [message.to_dict() for message in messages]
        return result

    async def _list_admin_user_ids(self, account_id: str, user_id: str = "") -> List[str]:
        if user_id:
            return [user_id]

        root_ctx = self._user_ctx(account_id, "system")
        try:
            entries = await self._viking_fs.ls("viking://session", ctx=root_ctx)
        except Exception:
            entries = []
        return sorted(
            {
                entry.get("name", "")
                for entry in entries
                if isinstance(entry, dict)
                and entry.get("name") not in {"", ".", ".."}
            }
        )

    async def _read_admin_session_meta_summary(
        self,
        account_id: str,
        user_id: str,
        item: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        sid = item.get("session_id", "")
        uri = item.get("uri", "")
        if not sid or not uri:
            return None

        target_ctx = self._user_ctx(account_id, user_id)
        meta: Optional[SessionMeta] = None
        try:
            content = await self._viking_fs.read_file(f"{uri}/.meta.json", ctx=target_ctx)
            raw_meta = json.loads(content)
            if "audit_summary" in raw_meta:
                meta = SessionMeta.from_dict(raw_meta)
        except Exception:
            meta = None

        if meta and self._is_current_audit_summary(meta):
            return self._admin_session_summary_result(
                account_id,
                user_id,
                sid,
                uri,
                meta,
            )

        return await self._backfill_admin_session_meta_summary(
            account_id,
            user_id,
            sid,
            target_ctx,
        )

    def _is_current_audit_summary(self, meta: SessionMeta) -> bool:
        summary = meta.audit_summary
        if (
            not meta.audit_summary_complete
            or meta.audit_summary_version < AUDIT_SUMMARY_VERSION
        ):
            return False
        return not (
            int(summary.get("message_count", 0) or 0) <= 0
            and int(meta.message_count or 0) > 0
        )

    def _admin_session_summary_result(
        self,
        account_id: str,
        user_id: str,
        session_id: str,
        uri: str,
        meta: SessionMeta,
    ) -> Dict[str, Any]:
        summary = meta.audit_summary
        return {
            **meta.to_dict(),
            "account_id": account_id,
            "user_id": user_id,
            "session_id": session_id,
            "uri": uri,
            "matched_message_count": int(summary.get("message_count", 0) or 0),
            "message_count": int(summary.get("message_count", 0) or 0),
            "user_message_count": int(summary.get("user_message_count", 0) or 0),
            "assistant_message_count": int(
                summary.get("assistant_message_count", 0) or 0
            ),
            "tool_call_count": int(summary.get("tool_call_count", 0) or 0),
            "failed_tool_call_count": int(
                summary.get("failed_tool_call_count", 0) or 0
            ),
            "context_ref_count": int(summary.get("context_ref_count", 0) or 0),
            "token_usage": dict(summary.get("token_usage", {})),
            "first_message_at": summary.get("first_message_at", ""),
            "last_message_at": summary.get("last_message_at", ""),
        }

    async def _backfill_admin_session_meta_summary(
        self,
        account_id: str,
        user_id: str,
        session_id: str,
        target_ctx: RequestContext,
    ) -> Optional[Dict[str, Any]]:
        try:
            session = await self.get(session_id, target_ctx)
            messages = await self._read_all_session_messages(session)
        except Exception as e:
            logger.debug(
                "Failed to backfill admin session summary %s/%s: %s",
                user_id,
                session_id,
                e,
            )
            return None

        session.meta.audit_summary = self._summarize_messages(messages)
        session.meta.audit_summary_version = AUDIT_SUMMARY_VERSION
        session.meta.audit_summary_complete = True
        try:
            await self._viking_fs.write_file(
                f"{session.uri}/.meta.json",
                json.dumps(session.meta.to_dict(), ensure_ascii=False),
                ctx=target_ctx,
            )
        except Exception as e:
            logger.debug(
                "Failed to persist backfilled admin session summary %s/%s: %s",
                user_id,
                session_id,
                e,
            )

        return self._admin_session_summary_result(
            account_id,
            user_id,
            session_id,
            session.uri,
            session.meta,
        )

    async def _collect_admin_session_summaries(
        self,
        account_id: str,
        *,
        user_id: str = "",
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Collect admin session summaries from current metadata only."""
        self._ensure_initialized()
        sessions: List[Dict[str, Any]] = []

        for uid in await self._list_admin_user_ids(account_id, user_id):
            target_ctx = self._user_ctx(account_id, uid)
            for item in await self.sessions(target_ctx):
                sid = item.get("session_id", "")
                if not sid:
                    continue

                detail = await self._read_admin_session_meta_summary(
                    account_id, uid, item
                )
                if detail is None:
                    logger.debug(
                        "Skipped admin session %s/%s without current audit summary",
                        uid,
                        sid,
                    )
                    continue

                activity_dt = _parse_datetime(_session_activity_value(detail))
                if from_date and activity_dt and activity_dt < from_date:
                    continue
                if to_date and activity_dt and activity_dt > to_date:
                    continue

                detail.pop("messages", None)
                sessions.append(detail)

        sessions.sort(key=_admin_session_sort_key)
        return sessions

    async def _collect_admin_sessions(
        self,
        account_id: str,
        *,
        user_id: str = "",
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        query: str = "",
    ) -> List[Dict[str, Any]]:
        """Collect filtered admin session summaries before pagination."""
        self._ensure_initialized()

        sessions: List[Dict[str, Any]] = []
        for uid in await self._list_admin_user_ids(account_id, user_id):
            target_ctx = self._user_ctx(account_id, uid)
            for item in await self.sessions(target_ctx):
                sid = item.get("session_id", "")
                if not sid:
                    continue
                try:
                    detail = await self.get_admin_session_detail(
                        account_id,
                        uid,
                        sid,
                        include_messages=bool(query),
                        query=query,
                    )
                except Exception as e:
                    logger.debug("Failed to read admin session %s/%s: %s", uid, sid, e)
                    continue

                if query and detail.get("matched_message_count", 0) == 0:
                    continue

                activity_dt = _parse_datetime(
                    detail.get("last_message_at")
                    or detail.get("updated_at")
                    or detail.get("created_at")
                )
                if from_date and activity_dt and activity_dt < from_date:
                    continue
                if to_date and activity_dt and activity_dt > to_date:
                    continue

                detail.pop("messages", None)
                sessions.append(detail)

        sessions.sort(key=_admin_session_sort_key)
        return sessions

    async def list_admin_sessions_paginated(
        self,
        account_id: str,
        *,
        user_id: str = "",
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        query: str = "",
        sort_by: str = DEFAULT_ADMIN_SESSION_SORT_BY,
        sort_order: str = DEFAULT_ADMIN_SESSION_SORT_ORDER,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """List sessions with pagination metadata for admin audit views."""
        if query:
            sessions = await self._collect_admin_sessions(
                account_id,
                user_id=user_id,
                from_date=from_date,
                to_date=to_date,
                query=query,
            )
        else:
            sessions = await self._collect_admin_session_summaries(
                account_id,
                user_id=user_id,
                from_date=from_date,
                to_date=to_date,
            )
        normalized_sort_by = (
            sort_by if sort_by in ADMIN_SESSION_SORT_FIELDS else DEFAULT_ADMIN_SESSION_SORT_BY
        )
        normalized_sort_order = (
            sort_order.lower() if sort_order.lower() in {"asc", "desc"} else DEFAULT_ADMIN_SESSION_SORT_ORDER
        )
        _sort_admin_sessions(
            sessions,
            sort_by=normalized_sort_by,
            sort_order=normalized_sort_order,
        )
        safe_page_size = max(1, min(page_size, 1000))
        total = len(sessions)
        total_pages = (total + safe_page_size - 1) // safe_page_size if total else 0
        safe_page = max(1, page)
        if total_pages:
            safe_page = min(safe_page, total_pages)
        else:
            safe_page = 1
        start = (safe_page - 1) * safe_page_size
        end = start + safe_page_size
        return {
            "items": sessions[start:end],
            "page": safe_page,
            "page_size": safe_page_size,
            "total": total,
            "total_pages": total_pages,
            "has_prev": safe_page > 1 and total > 0,
            "has_next": safe_page < total_pages,
            "sort_by": normalized_sort_by,
            "sort_order": normalized_sort_order,
        }

    async def get_admin_daily_analytics(
        self,
        account_id: str,
        *,
        user_id: str = "",
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        tz: tzinfo = timezone.utc,
    ) -> Dict[str, Any]:
        """Aggregate daily users, sessions, messages, tools, and tokens."""
        if from_date is None:
            from_date = datetime.combine(
                datetime.now(tz).date() - timedelta(days=13),
                time.min,
                tzinfo=tz,
            )
        if to_date is None:
            to_date = datetime.combine(
                datetime.now(tz).date(),
                time.max,
                tzinfo=tz,
            )

        sessions = await self._collect_admin_session_summaries(
            account_id,
            user_id=user_id,
            from_date=from_date,
            to_date=to_date,
        )

        by_day: Dict[str, Dict[str, Any]] = {}
        for session_info in sessions:
            day = _date_key(
                _parse_datetime(
                    session_info.get("last_message_at")
                    or session_info.get("updated_at")
                    or session_info.get("created_at")
                ),
                tz,
            )
            if day not in by_day:
                by_day[day] = _empty_daily_analytics_row(day)

            bucket = by_day[day]
            bucket["active_users"].add(session_info["user_id"])
            bucket["session_count"] += 1
            bucket["message_count"] += int(session_info.get("message_count", 0) or 0)
            bucket["user_message_count"] += int(
                session_info.get("user_message_count", 0) or 0
            )
            bucket["assistant_message_count"] += int(
                session_info.get("assistant_message_count", 0) or 0
            )
            bucket["tool_call_count"] += int(session_info.get("tool_call_count", 0) or 0)
            bucket["failed_tool_call_count"] += int(
                session_info.get("failed_tool_call_count", 0) or 0
            )
            _merge_token_usage(bucket["token_usage"], session_info.get("token_usage", {}))

        daily = []
        totals = {
            "active_users": set(),
            "session_count": 0,
            "message_count": 0,
            "user_message_count": 0,
            "assistant_message_count": 0,
            "tool_call_count": 0,
            "failed_tool_call_count": 0,
            "token_usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
            },
        }
        start_day = from_date.astimezone(tz).date()
        end_day = to_date.astimezone(tz).date()
        if start_day <= end_day:
            day_count = (end_day - start_day).days + 1
            if day_count <= MAX_ADMIN_DAILY_BUCKETS:
                day_keys = [
                    (start_day + timedelta(days=offset)).isoformat()
                    for offset in range(day_count)
                ]
            else:
                day_keys = sorted(by_day)
        else:
            day_keys = sorted(by_day)

        for day in day_keys:
            by_day.setdefault(day, _empty_daily_analytics_row(day))
            item = by_day[day]
            users = item["active_users"]
            totals["active_users"].update(users)
            for key in [
                "session_count",
                "message_count",
                "user_message_count",
                "assistant_message_count",
                "tool_call_count",
                "failed_tool_call_count",
            ]:
                totals[key] += item[key]
            _merge_token_usage(totals["token_usage"], item["token_usage"])
            daily.append({**item, "active_users": len(users)})

        return {
            "account_id": account_id,
            "user_id": user_id,
            "from": from_date.date().isoformat() if from_date else "",
            "to": to_date.date().isoformat() if to_date else "",
            "timezone": getattr(tz, "key", str(tz)),
            "daily": daily,
            "totals": {**totals, "active_users": len(totals["active_users"])},
        }

    async def delete(self, session_id: str, ctx: RequestContext) -> bool:
        """Delete a session.

        Args:
            session_id: Session ID to delete

        Returns:
            True if deleted successfully
        """
        self._ensure_initialized()
        session_uri = f"viking://session/{ctx.user.user_space_name()}/{session_id}"

        try:
            await self._viking_fs.rm(session_uri, recursive=True, ctx=ctx)
            logger.info(f"Deleted session: {session_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to delete session {session_id}: {e}")
            raise NotFoundError(session_id, "session")

    async def delete_admin_session(
        self,
        account_id: str,
        user_id: str,
        session_id: str,
    ) -> bool:
        """Delete a target user's session for admin audit endpoints."""
        return await self.delete(session_id, self._user_ctx(account_id, user_id))

    async def commit(
        self,
        session_id: str,
        ctx: RequestContext,
        *,
        memory_scope: str = ALL_MEMORY_SCOPE,
    ) -> Dict[str, Any]:
        """Commit a session (archive messages and extract memories).

        Delegates to commit_async() for true non-blocking behavior.

        Args:
            session_id: Session ID to commit

        Returns:
            Commit result
        """
        return await self.commit_async(session_id, ctx, memory_scope=memory_scope)

    async def commit_async(
        self,
        session_id: str,
        ctx: RequestContext,
        *,
        memory_scope: str = ALL_MEMORY_SCOPE,
    ) -> Dict[str, Any]:
        """Async commit a session.

        Phase 1 (archive) always runs inline.  Phase 2 (memory extraction)
        runs in a background task, returning a task_id for polling.

        Args:
            session_id: Session ID to commit

        Returns:
            Commit result with keys: session_id, status, task_id,
            archive_uri, archived
        """
        self._ensure_initialized()
        session = await self.get(session_id, ctx)
        return await session.commit_async(memory_scope=normalize_memory_scope(memory_scope))

    async def get_commit_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Query background commit task status by task_id."""
        task = get_task_tracker().get(task_id)
        return task.to_dict() if task else None

    async def extract(
        self,
        session_id: str,
        ctx: RequestContext,
        *,
        memory_scope: str = ALL_MEMORY_SCOPE,
    ) -> List[Any]:
        """Extract memories from a session.

        Args:
            session_id: Session ID to extract from

        Returns:
            List of extracted memories
        """
        self._ensure_initialized()
        if not self._session_compressor:
            raise NotInitializedError("SessionCompressor")

        session = await self.get(session_id, ctx)

        return await self._session_compressor.extract_long_term_memories(
            messages=session.messages,
            user=ctx.user,
            session_id=session_id,
            ctx=ctx,
            memory_scope=normalize_memory_scope(memory_scope),
        )
