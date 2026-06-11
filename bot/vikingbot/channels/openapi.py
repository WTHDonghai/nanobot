"""OpenAPI channel for HTTP-based chat API."""

import asyncio
import html
import mimetypes
import os
import re
import secrets
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from loguru import logger

from openviking_cli.resource_preview import (
    RESOURCE_PREVIEW_SECRET_ENV,
    ResourcePreviewTokenError,
    verify_resource_preview_token,
)
from vikingbot.bus.events import InboundMessage, OutboundEventType, OutboundMessage
from vikingbot.bus.queue import MessageBus
from vikingbot.channels.base import BaseChannel
from vikingbot.channels.openapi_models import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    EventType,
    HealthResponse,
    HumanHandoffRequest,
    HumanHandoffResponse,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionDetailResponse,
    SessionInfo,
    SessionListResponse,
)
from vikingbot.config.schema import BaseChannelConfig, Config, SessionKey
from vikingbot.openviking_mount.ov_server import VikingClient
from vikingbot.services.human_handoff import HumanHandoffPayload, HumanHandoffService
from vikingbot.utils import get_images_path


class OpenAPIChannelConfig(BaseChannelConfig):
    """Configuration for OpenAPI channel."""

    enabled: bool = True
    type: str = "cli"
    api_key: str = ""  # If empty, no auth required
    allow_from: list[str] = []
    max_concurrent_requests: int = 100
    base_url: str = ""  # Optional prefix for bot image and resource preview URLs (e.g. "https://api.yoursite.com").
    _channel_id: str = "default"

    def channel_id(self) -> str:
        return self._channel_id


class PendingResponse:
    """Tracks a pending response from the agent."""

    def __init__(self):
        self.events: List[Dict[str, Any]] = []
        self.final_content: Optional[str] = None
        self.event = asyncio.Event()
        self.stream_queue: asyncio.Queue[Optional[ChatStreamEvent]] = asyncio.Queue()

    async def add_event(self, event_type: str, data: Any):
        """Add an event to the response."""
        event = {"type": event_type, "data": data, "timestamp": datetime.now().isoformat()}
        self.events.append(event)
        await self.stream_queue.put(ChatStreamEvent(event=EventType(event_type), data=data))

    def set_final(self, content: str):
        """Set the final response content."""
        self.final_content = content
        self.event.set()

    async def close_stream(self):
        """Close the stream queue."""
        await self.stream_queue.put(None)


class OpenAPIChannel(BaseChannel):
    """
    OpenAPI channel exposing HTTP endpoints for chat API.
    This channel works differently from others - it doesn't subscribe
    to outbound messages directly but uses request-response pattern.
    """

    name: str = "openapi"

    def __init__(
        self,
        config: OpenAPIChannelConfig,
        bus: MessageBus,
        workspace_path: Path | None = None,
        app: "FastAPI | None" = None,
        bot_config: Config | None = None,
    ):
        super().__init__(config, bus, workspace_path)
        self.config = config
        self.bot_config = bot_config or Config()
        self._pending: Dict[str, PendingResponse] = {}
        self._sessions: Dict[str, Dict[str, Any]] = {}
        self._router: Optional[APIRouter] = None
        self._app = app  # External FastAPI app to register routes on
        self._server: Optional[asyncio.Task] = None  # Server task
        self._human_handoff_service = HumanHandoffService(self.bot_config.tools.human_handoff)

    async def start(self) -> None:
        """Start the channel - register routes to external FastAPI app if provided."""
        self._running = True

        # Register routes to external FastAPI app
        if self._app is not None:
            self._setup_routes()

        logger.info("OpenAPI channel started")

    async def stop(self) -> None:
        """Stop the channel."""
        self._running = False
        # Complete all pending responses
        for pending in self._pending.values():
            pending.set_final("")
        logger.info("OpenAPI channel stopped")

    def _replace_bot_resource_links(self, content: str) -> str:
        """
        Replace bot-local Markdown references with URLs served by this channel.

        Handles two forms:
          - Markdown image:  ![alt](send://foo.png)  →  ![alt](/bot/v1/images/foo.png)
          - Bare reference:  send://foo.png           →  ![foo](/bot/v1/images/foo.png)
          - Markdown link:   [doc](/bot/v1/resources/preview?uri=...) → absolute URL when base_url is set
        """
        if not content:
            return content

        base = self.config.base_url.rstrip("/") if self.config.base_url else ""

        def _image_url(filename: str) -> str:
            return f"{base}/bot/v1/images/{filename}"

        def _replace_markdown(m: re.Match) -> str:
            alt, ref = m.group(1), m.group(2)
            filename = ref[len("send://"):]
            if not (images_path / filename).exists():
                logger.warning(f"OpenAPI channel: image file not found, skipping: {filename}")
                return m.group(0)  # leave as-is
            return f"![{alt}]({_image_url(filename)})"

        def _replace_bare(m: re.Match) -> str:
            ref = m.group(0)
            filename = ref[len("send://"):]
            if not (images_path / filename).exists():
                logger.warning(f"OpenAPI channel: image file not found, skipping: {filename}")
                return ref
            alt = filename.rsplit(".", 1)[0]
            return f"![{alt}]({_image_url(filename)})"

        result = content
        if "send://" in result:
            images_path = get_images_path()
            # First replace Markdown images that point to send://
            result = re.sub(
                r"!\[([^\]]*)\]\((send://[^)\s]+)\)",
                _replace_markdown,
                result,
            )
            # Then replace any remaining bare send:// references
            result = re.sub(
                r"send://[^\s)>\"']+",
                _replace_bare,
                result,
            )
        if base and "/bot/v1/resources/preview?" in result:
            result = re.sub(
                r"\]\((/bot/v1/resources/preview\?[^)\s]+)\)",
                lambda m: f"]({base}{m.group(1)})",
                result,
            )
        return result

    async def send(self, msg: OutboundMessage) -> None:
        """
        Handle outbound messages - routes to pending responses.
        This is called by the message bus dispatcher.
        """
        session_id = msg.session_key.chat_id
        pending = self._pending.get(session_id)

        if not pending:
            # No pending request for this session, ignore
            return

        if msg.event_type == OutboundEventType.RESPONSE:
            # Rewrite send:// image references before delivering to clients
            content = self._replace_bot_resource_links(msg.content or "")
            await pending.add_event("response", content)
            pending.set_final(content)
            await pending.close_stream()
        elif msg.event_type == OutboundEventType.REASONING:
            await pending.add_event("reasoning", msg.content)
        elif msg.event_type == OutboundEventType.ITERATION:
            await pending.add_event("iteration", msg.content)
        elif msg.event_type == OutboundEventType.TOOL_CALL:
            await pending.add_event("tool_call", msg.content)
        elif msg.event_type == OutboundEventType.TOOL_RESULT:
            # Also rewrite images inside tool results so streaming clients see valid URLs
            content = self._replace_bot_resource_links(msg.content or "")
            await pending.add_event("tool_result", content)

    def get_router(self) -> APIRouter:
        """Get or create the FastAPI router."""
        if self._router is None:
            self._router = self._create_router()
        return self._router

    def _create_router(self) -> APIRouter:
        """Create the FastAPI router with all routes."""
        router = APIRouter()
        channel = self  # Capture for closures

        async def verify_api_key(x_api_key: Optional[str] = Header(None)) -> bool:
            """Verify API key if configured."""
            if not channel.config.api_key:
                return True  # No auth required
            if not x_api_key:
                raise HTTPException(status_code=401, detail="X-API-Key header required")
            # Use secrets.compare_digest for timing-safe comparison
            if not secrets.compare_digest(x_api_key, channel.config.api_key):
                raise HTTPException(status_code=403, detail="Invalid API key")
            return True

        @router.get("/health", response_model=HealthResponse)
        async def health_check():
            """Health check endpoint."""
            from vikingbot import __version__

            return HealthResponse(
                status="healthy" if channel._running else "unhealthy",
                version=__version__,
            )

        @router.get("/images/{image_name:path}")
        async def serve_image(image_name: str):
            """
            Serve a bot-generated image by name.
            Images are stored under get_data_path()/images/ and referenced
            via send:// URIs inside agent responses.
            """
            # Security: reject any path that tries to escape the images directory
            if ".." in image_name or image_name.startswith("/"):
                raise HTTPException(status_code=400, detail="Invalid image name")

            image_path = get_images_path() / image_name
            if not image_path.exists() or not image_path.is_file():
                raise HTTPException(status_code=404, detail="Image not found")

            media_type, _ = mimetypes.guess_type(str(image_path))
            return FileResponse(
                path=str(image_path),
                media_type=media_type or "application/octet-stream",
                filename=image_path.name,
            )

        @router.get("/resources/preview")
        async def preview_resource(
            request: Request,
            uri: str = Query(..., description="Viking document URI"),
            token: str = Query(..., description="Signed resource preview capability"),
        ):
            """Render a referenced OpenViking document as a lightweight preview page."""
            normalized_uri = uri.strip().rstrip("/")
            if not normalized_uri.startswith("viking://resources/"):
                raise HTTPException(status_code=400, detail="Invalid resource URI")
            preview_secret = (
                os.environ.get(RESOURCE_PREVIEW_SECRET_ENV, "").strip()
                or str(channel.bot_config.ov_server.root_api_key or "").strip()
            )
            try:
                claims = verify_resource_preview_token(
                    token,
                    secret=preview_secret,
                    expected_uri=normalized_uri,
                )
            except ResourcePreviewTokenError:
                raise HTTPException(status_code=404, detail="Resource preview is not available")
            if claims.account_id != channel.bot_config.ov_server.account_id:
                raise HTTPException(status_code=404, detail="Resource preview is not available")

            client = await VikingClient.create()
            try:
                stat = await client.stat(normalized_uri)
                title = str(
                    stat.get("name") or normalized_uri.rsplit("/", 1)[-1] or normalized_uri
                )
                content = await client.read_content(normalized_uri, level="read")
                if not str(content or "").strip():
                    raise HTTPException(status_code=404, detail="Resource content not found")
                try:
                    content = await client.materialize_inline_image_refs(
                        str(content), normalized_uri
                    )
                except Exception as e:
                    logger.warning(f"Failed to materialize preview images for {normalized_uri}: {e}")
                content = channel._replace_bot_resource_links(str(content))
            finally:
                await client.close()

            if "application/json" in request.headers.get("accept", ""):
                return JSONResponse(
                    {
                        "title": title,
                        "uri": normalized_uri,
                        "markdown": str(content),
                    }
                )

            escaped_title = html.escape(title)
            escaped_uri = html.escape(normalized_uri)
            escaped_content = html.escape(str(content))
            page = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{escaped_title}</title>
  <style>
    body {{
      margin: 0;
      background: #f7f7f8;
      color: #1f2328;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.65;
    }}
    main {{
      max-width: 960px;
      margin: 0 auto;
      padding: 32px 20px 48px;
    }}
    h1 {{
      margin: 0 0 8px;
      font-size: 24px;
      line-height: 1.25;
    }}
    .uri {{
      margin: 0 0 20px;
      color: #636c76;
      font-size: 13px;
      overflow-wrap: anywhere;
    }}
    pre {{
      margin: 0;
      padding: 20px;
      background: #fff;
      border: 1px solid #d0d7de;
      border-radius: 8px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      font: inherit;
    }}
  </style>
</head>
<body>
  <main>
    <h1>{escaped_title}</h1>
    <p class="uri">{escaped_uri}</p>
    <pre>{escaped_content}</pre>
  </main>
</body>
</html>"""
            return HTMLResponse(page)

        @router.post("/chat", response_model=ChatResponse)
        async def chat(
            request: ChatRequest,
            authorized: bool = Depends(verify_api_key),
        ):
            """Send a chat message and get a response."""
            return await channel._handle_chat(request)

        @router.post("/chat/stream")
        async def chat_stream(
            request: ChatRequest,
            authorized: bool = Depends(verify_api_key),
        ):
            """Send a chat message and get a streaming response."""
            if not request.stream:
                request.stream = True
            return await channel._handle_chat_stream(request)

        @router.post("/handoff", response_model=HumanHandoffResponse)
        async def handoff(
            request: HumanHandoffRequest,
            authorized: bool = Depends(verify_api_key),
        ):
            """Create a human support handoff for the current session."""
            return await channel._handle_handoff(request)

        @router.get("/sessions", response_model=SessionListResponse)
        async def list_sessions(
            authorized: bool = Depends(verify_api_key),
        ):
            """List all sessions."""
            sessions = []
            for session_id, session_data in channel._sessions.items():
                sessions.append(
                    SessionInfo(
                        id=session_id,
                        created_at=session_data.get("created_at", datetime.now()),
                        last_active=session_data.get("last_active", datetime.now()),
                        message_count=session_data.get("message_count", 0),
                    )
                )
            return SessionListResponse(sessions=sessions, total=len(sessions))

        @router.post("/sessions", response_model=SessionCreateResponse)
        async def create_session(
            request: SessionCreateRequest,
            authorized: bool = Depends(verify_api_key),
        ):
            """Create a new session."""
            session_id = str(uuid.uuid4())
            now = datetime.now()
            channel._sessions[session_id] = {
                "user_id": request.user_id,
                "created_at": now,
                "last_active": now,
                "message_count": 0,
                "metadata": request.metadata or {},
            }
            return SessionCreateResponse(session_id=session_id, created_at=now)

        @router.get("/sessions/{session_id}", response_model=SessionDetailResponse)
        async def get_session(
            session_id: str,
            authorized: bool = Depends(verify_api_key),
        ):
            """Get session details."""
            if session_id not in channel._sessions:
                raise HTTPException(status_code=404, detail="Session not found")

            session_data = channel._sessions[session_id]
            info = SessionInfo(
                id=session_id,
                created_at=session_data.get("created_at", datetime.now()),
                last_active=session_data.get("last_active", datetime.now()),
                message_count=session_data.get("message_count", 0),
            )
            # Get messages from session manager if available
            messages = session_data.get("messages", [])
            return SessionDetailResponse(session=info, messages=messages)

        @router.delete("/sessions/{session_id}")
        async def delete_session(
            session_id: str,
            authorized: bool = Depends(verify_api_key),
        ):
            """Delete a session."""
            if session_id not in channel._sessions:
                raise HTTPException(status_code=404, detail="Session not found")

            del channel._sessions[session_id]
            return {"deleted": True}

        return router

    def _setup_routes(self) -> None:
        """Setup routes on the external FastAPI app."""
        if self._app is None:
            logger.warning("No external FastAPI app provided, cannot setup routes")
            return

        # Get the router and include it at root path
        # Note: openviking-server adds its own /bot/v1 prefix when proxying
        router = self.get_router()
        self._app.include_router(router, prefix="/bot/v1")
        logger.info("OpenAPI routes registered at root path")

    async def _handle_chat(self, request: ChatRequest) -> ChatResponse:
        """Handle a chat request."""
        # Generate or use provided session ID
        session_id = request.session_id or str(uuid.uuid4())
        user_id = request.user_id or "anonymous"

        # Create session if new
        if session_id not in self._sessions:
            self._sessions[session_id] = {
                "user_id": user_id,
                "created_at": datetime.now(),
                "last_active": datetime.now(),
                "message_count": 0,
                "messages": [],
            }

        # Update session activity
        self._sessions[session_id]["last_active"] = datetime.now()
        self._sessions[session_id]["message_count"] += 1

        # Create pending response tracker
        pending = PendingResponse()
        self._pending[session_id] = pending

        try:
            # Build session key
            session_key = SessionKey(
                type="cli",
                channel_id=self.config.channel_id(),
                chat_id=session_id,
            )

            # Build content with context if provided
            content = request.message
            if request.context:
                # Context is handled separately by session manager
                pass

            # Create and publish inbound message
            msg = InboundMessage(
                session_key=session_key,
                sender_id=user_id,
                content=content,
                metadata={"openviking_session_id": session_id},
            )

            await self.bus.publish_inbound(msg)

            # Wait for response with timeout
            try:
                await asyncio.wait_for(pending.event.wait(), timeout=300.0)
            except asyncio.TimeoutError:
                raise HTTPException(status_code=504, detail="Request timeout")

            # Build response
            response_content = pending.final_content or ""

            return ChatResponse(
                session_id=session_id,
                message=response_content,
                events=pending.events if pending.events else None,
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Error handling chat request: {e}")
            raise HTTPException(status_code=500, detail=f"Internal error: {str(e)}")
        finally:
            # Clean up pending
            self._pending.pop(session_id, None)

    async def _handle_chat_stream(self, request: ChatRequest) -> StreamingResponse:
        """Handle a streaming chat request."""
        session_id = request.session_id or str(uuid.uuid4())
        user_id = request.user_id or "anonymous"

        # Create session if new
        if session_id not in self._sessions:
            self._sessions[session_id] = {
                "user_id": user_id,
                "created_at": datetime.now(),
                "last_active": datetime.now(),
                "message_count": 0,
                "messages": [],
            }

        self._sessions[session_id]["last_active"] = datetime.now()
        self._sessions[session_id]["message_count"] += 1

        pending = PendingResponse()
        self._pending[session_id] = pending

        async def event_generator():
            try:
                # Emit an immediate progress event so streaming clients receive a first SSE
                # packet before queueing, classification, or the first model/tool round finishes.
                await pending.add_event("reasoning", "Request received. Preparing context...")

                # Build session key and send message
                session_key = SessionKey(
                    type="cli",
                    channel_id=self.config.channel_id(),
                    chat_id=session_id,
                )

                msg = InboundMessage(
                    session_key=session_key,
                    sender_id=user_id,
                    content=request.message,
                    metadata={"openviking_session_id": session_id},
                )

                await self.bus.publish_inbound(msg)

                # Stream events as they arrive
                while True:
                    try:
                        event = await asyncio.wait_for(pending.stream_queue.get(), timeout=300.0)
                        if event is None:
                            break
                        yield f"data: {event.model_dump_json()}\n\n"
                    except asyncio.TimeoutError:
                        yield f"data: {ChatStreamEvent(event=EventType.RESPONSE, data={'error': 'timeout'}).model_dump_json()}\n\n"
                        break

            except Exception as e:
                logger.exception(f"Error in stream generator: {e}")
                error_event = ChatStreamEvent(event=EventType.RESPONSE, data={"error": str(e)})
                yield f"data: {error_event.model_dump_json()}\n\n"
            finally:
                self._pending.pop(session_id, None)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    async def _handle_handoff(self, request: HumanHandoffRequest) -> HumanHandoffResponse:
        """Handle a human handoff request."""
        session_data = self._sessions.get(request.session_id or "", {})
        result = await self._human_handoff_service.request_handoff(
            HumanHandoffPayload(
                session_id=request.session_id,
                user_id=request.user_id or session_data.get("user_id"),
                reason=request.reason,
                summary=request.summary,
                latest_user_message=request.latest_user_message,
                latest_assistant_message=request.latest_assistant_message,
                source=request.source,
                metadata={
                    "channel_type": "cli",
                    "channel_id": self.config.channel_id(),
                    **request.metadata,
                },
            )
        )
        return HumanHandoffResponse(
            success=result.success,
            status=result.status,
            message=result.message,
            handoff_id=result.handoff_id,
            entry_url=result.entry_url,
            service_response=result.service_response,
        )


def get_openapi_router(bus: MessageBus, config: Config) -> APIRouter:
    """
    Create and return the OpenAPI router for mounting in FastAPI.

    This factory function creates an OpenAPIChannel and returns its router.
    The router should be mounted in the main FastAPI app.
    """
    # Find OpenAPI config from channels
    openapi_config = None
    for ch_config in config.channels:
        if isinstance(ch_config, dict) and ch_config.get("type") == "openapi":
            openapi_config = OpenAPIChannelConfig(**ch_config)
            break
        elif hasattr(ch_config, "type") and getattr(ch_config, "type", None) == "openapi":
            openapi_config = ch_config
            break

    if openapi_config is None:
        # Create default config
        openapi_config = OpenAPIChannelConfig()

    # Create channel and get router
    channel = OpenAPIChannel(
        config=openapi_config,
        bus=bus,
        workspace_path=config.workspace_path,
        bot_config=config,
    )

    # Register channel's send method as subscriber for outbound messages
    bus.subscribe_outbound(
        f"cli__{openapi_config.channel_id()}",
        channel.send,
    )

    return channel.get_router()
