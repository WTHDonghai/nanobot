# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Sessions endpoints for OpenViking HTTP Server."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, model_validator

from openviking.message.part import TextPart, part_from_dict
from openviking.server.auth import get_request_context
from openviking.server.dependencies import get_service
from openviking.server.identity import RequestContext
from openviking.server.models import ErrorInfo, Response

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])
logger = logging.getLogger(__name__)


class TextPartRequest(BaseModel):
    """Text part request model."""

    type: Literal["text"] = "text"
    text: str


class ContextPartRequest(BaseModel):
    """Context part request model."""

    type: Literal["context"] = "context"
    uri: str = ""
    context_type: Literal["memory", "resource", "skill"] = "memory"
    abstract: str = ""


class ToolPartRequest(BaseModel):
    """Tool part request model."""

    type: Literal["tool"] = "tool"
    tool_id: str = ""
    tool_name: str = ""
    tool_uri: str = ""
    skill_uri: str = ""
    tool_input: Optional[Dict[str, Any]] = None
    tool_output: str = ""
    tool_status: str = "pending"


PartRequest = TextPartRequest | ContextPartRequest | ToolPartRequest


class AddMessageRequest(BaseModel):
    """Request model for adding a message.

    Supports two input modes:
    1. Simple input: provide `content` string, stored as a text part
    2. Parts input: provide `parts` array for full Part support

    If both are provided, `parts` takes precedence.
    """

    role: str
    content: Optional[str] = None
    parts: Optional[List[Dict[str, Any]]] = None
    created_at: Optional[str] = None
    token_usage: Optional[Dict[str, int]] = None
    metadata: Optional[Dict[str, Any]] = None

    @model_validator(mode="after")
    def validate_content_or_parts(self) -> "AddMessageRequest":
        if self.content is None and self.parts is None:
            raise ValueError("Either 'content' or 'parts' must be provided")
        return self


class UsedRequest(BaseModel):
    """Request model for recording usage."""

    contexts: Optional[List[str]] = None
    skill: Optional[Dict[str, Any]] = None


class FeedbackRequest(BaseModel):
    """One-click message feedback."""

    value: Literal["up", "down"]
    reason_tags: Optional[List[str]] = None
    comment: Optional[str] = None


class CommitSessionRequest(BaseModel):
    """Request model for committing a session."""

    telemetry: Optional[Any] = False
    memory_scope: Literal["all", "user", "agent"] = "all"


def _to_jsonable(value: Any) -> Any:
    """Convert internal objects (e.g. Context) into JSON-serializable values."""
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {k: _to_jsonable(v) for k, v in value.items()}
    return value


@router.post("")
async def create_session(
    _ctx: RequestContext = Depends(get_request_context),
):
    """Create a new session."""
    service = get_service()
    await service.initialize_user_directories(_ctx)
    await service.initialize_agent_directories(_ctx)
    session = await service.sessions.create(_ctx)
    return Response(
        status="ok",
        result={
            "session_id": session.session_id,
            "user": session.user.to_dict(),
        },
    )


@router.get("")
async def list_sessions(
    _ctx: RequestContext = Depends(get_request_context),
):
    """List all sessions."""
    service = get_service()
    result = await service.sessions.sessions(_ctx)
    return Response(status="ok", result=result)


@router.get("/{session_id}")
async def get_session(
    session_id: str = Path(..., description="Session ID"),
    auto_create: bool = Query(False, description="Create the session if it does not exist"),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Get session details."""
    from openviking_cli.exceptions import NotFoundError

    service = get_service()
    try:
        session = await service.sessions.get(session_id, _ctx, auto_create=auto_create)
    except NotFoundError:
        return Response(
            status="error",
            error=ErrorInfo(code="NOT_FOUND", message=f"Session {session_id} not found"),
        )
    result = session.meta.to_dict()
    result["user"] = session.user.to_dict()
    pending_tokens = sum(len(m.content) // 4 for m in session.messages)
    result["pending_tokens"] = pending_tokens
    return Response(status="ok", result=result)


@router.get("/{session_id}/context")
async def get_session_context(
    session_id: str = Path(..., description="Session ID"),
    token_budget: int = Query(128_000, description="Token budget for session context"),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Get assembled session context."""
    service = get_service()
    session = service.sessions.session(_ctx, session_id)
    await session.load()
    result = await session.get_session_context(token_budget=token_budget)
    return Response(status="ok", result=_to_jsonable(result))


@router.get("/{session_id}/archives/{archive_id}")
async def get_session_archive(
    session_id: str = Path(..., description="Session ID"),
    archive_id: str = Path(..., description="Archive ID"),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Get one completed archive for a session."""
    from openviking_cli.exceptions import NotFoundError

    service = get_service()
    session = service.sessions.session(_ctx, session_id)
    await session.load()
    try:
        result = await session.get_session_archive(archive_id)
    except NotFoundError:
        return Response(
            status="error",
            error=ErrorInfo(code="NOT_FOUND", message=f"Archive {archive_id} not found"),
        )
    return Response(status="ok", result=_to_jsonable(result))


@router.delete("/{session_id}")
async def delete_session(
    session_id: str = Path(..., description="Session ID"),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Delete a session."""
    service = get_service()
    await service.sessions.delete(session_id, _ctx)
    return Response(status="ok", result={"session_id": session_id})


@router.post("/{session_id}/commit")
async def commit_session(
    request: CommitSessionRequest | None = None,
    session_id: str = Path(..., description="Session ID"),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Commit a session (archive and extract memories).

    Archive (Phase 1) completes before returning.  Memory extraction
    (Phase 2) runs in the background.  A ``task_id`` is returned for
    polling progress via ``GET /tasks/{task_id}``.
    """
    service = get_service()
    memory_scope = request.memory_scope if request is not None else "all"
    result = await service.sessions.commit_async(
        session_id,
        _ctx,
        memory_scope=memory_scope,
    )
    return Response(status="ok", result=result).model_dump(exclude_none=True)


@router.post("/{session_id}/extract")
async def extract_session(
    session_id: str = Path(..., description="Session ID"),
    memory_scope: Literal["all", "user", "agent"] = Query(
        "all",
        description="Which memory categories to extract from the session",
    ),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Extract memories from a session."""
    service = get_service()
    result = await service.sessions.extract(session_id, _ctx, memory_scope=memory_scope)
    return Response(status="ok", result=_to_jsonable(result))


@router.post("/{session_id}/messages")
async def add_message(
    request: AddMessageRequest,
    session_id: str = Path(..., description="Session ID"),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Add a message to a session.

    Supports two input modes:
    1. Simple input: provide `content` string, stored as a text part
       Example: {"role": "user", "content": "Hello"}

    2. Parts input: provide `parts` array for full Part support
       Example: {"role": "assistant", "parts": [
           {"type": "text", "text": "Here's the answer"},
           {"type": "context", "uri": "viking://resources/doc.md", "abstract": "..."}
       ]}

    If both `content` and `parts` are provided, `parts` takes precedence.
    """
    service = get_service()
    session = service.sessions.session(_ctx, session_id)
    await session.load()

    if request.parts is not None:
        parts = [part_from_dict(p) for p in request.parts]
    else:
        parts = [TextPart(text=request.content or "")]

    # 解析 created_at
    created_at = None
    if request.created_at:
        try:
            created_at = datetime.fromisoformat(request.created_at)
        except ValueError:
            logger.warning(f"Invalid created_at format: {request.created_at}")

    message = session.add_message(
        request.role,
        parts,
        created_at=created_at,
        token_usage=request.token_usage,
        metadata=request.metadata,
    )
    return Response(
        status="ok",
        result={
            "session_id": session_id,
            "message_id": message.id,
            "message_count": len(session.messages),
        },
    )


@router.get("/{session_id}/feedback")
async def get_session_feedback(
    session_id: str = Path(..., description="Session ID"),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Get message feedback for a session."""
    service = get_service()
    session = service.sessions.session(_ctx, session_id)
    await session.load()
    return Response(
        status="ok",
        result={
            "session_id": session_id,
            "feedback": session.feedback,
            "summary": session.meta.feedback_summary,
        },
    )


@router.put("/{session_id}/messages/{message_id}/feedback")
async def set_message_feedback(
    request: FeedbackRequest,
    session_id: str = Path(..., description="Session ID"),
    message_id: str = Path(..., description="Message ID"),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Record one-click feedback for an assistant message."""
    service = get_service()
    session = service.sessions.session(_ctx, session_id)
    await session.load()
    try:
        entry = await session.set_message_feedback(
            message_id,
            request.value,
            reason_tags=request.reason_tags,
            comment=request.comment or "",
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=400,
            content=Response(
                status="error",
                error=ErrorInfo(code="INVALID_ARGUMENT", message=str(exc)),
            ).model_dump(),
        )

    return Response(
        status="ok",
        result={
            "session_id": session_id,
            "message_id": message_id,
            "feedback": entry,
            "summary": session.meta.feedback_summary,
        },
    )


@router.post("/{session_id}/used")
async def record_used(
    request: UsedRequest,
    session_id: str = Path(..., description="Session ID"),
    _ctx: RequestContext = Depends(get_request_context),
):
    """Record actually used contexts and skills in a session."""
    service = get_service()
    session = service.sessions.session(_ctx, session_id)
    await session.load()
    session.used(contexts=request.contexts, skill=request.skill)
    return Response(
        status="ok",
        result={
            "session_id": session_id,
            "contexts_used": session.stats.contexts_used,
            "skills_used": session.stats.skills_used,
        },
    )
