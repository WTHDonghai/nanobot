"""Bot API router for proxying requests to Vikingbot OpenAPIChannel.

This router provides endpoints for the Bot API that proxy requests to the
Vikingbot OpenAPIChannel when the --with-bot option is enabled.
"""

import json
import os
from typing import AsyncGenerator, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from openviking.server.auth import get_request_context
from openviking.server.identity import RequestContext, Role
from openviking_cli.resource_preview import (
    RESOURCE_PREVIEW_SECRET_ENV,
    ResourcePreviewTokenError,
    create_resource_preview_token,
    verify_resource_preview_token,
)
from openviking_cli.utils.logger import get_logger

router = APIRouter(prefix="", tags=["bot"])

logger = get_logger(__name__)

# Bot API configuration - set when --with-bot is enabled
BOT_API_URL: Optional[str] = None  # e.g., "http://localhost:18791"


def set_bot_api_url(url: str) -> None:
    """Set the Bot API URL. Called by app.py when --with-bot is enabled."""
    global BOT_API_URL
    BOT_API_URL = url


def get_bot_url() -> str:
    """Get the Bot API URL, raising 503 if not configured."""
    if BOT_API_URL is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Bot service not enabled. Start server with --with-bot option.",
        )
    return BOT_API_URL


def extract_auth_token(request: Request) -> Optional[str]:
    """Extract and return authorization token from request."""
    # Try X-API-Key header first
    api_key = request.headers.get("X-API-Key")
    if api_key:
        return api_key

    # Try Authorization header (Bearer token)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:]  # Remove "Bearer " prefix

    return None


def require_auth_token(request: Request) -> str:
    """Return an auth token or raise 401 for bot proxy endpoints."""
    # Check if auth is disabled (dev mode) via app state
    if hasattr(request.app, "state") and getattr(request.app.state, "api_key_manager", None) is None:
        if getattr(request.app.state, "config", None) and request.app.state.config.auth_mode != "trusted":
            return "dev_mode_dummy_token"

    auth_token = extract_auth_token(request)
    if not auth_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authentication token",
        )
    return auth_token


def _prepare_bot_request_body(body: object, ctx: RequestContext) -> dict:
    """Normalize proxied bot request payloads and enforce USER scoping."""
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be a JSON object",
        )

    proxied = dict(body)
    if ctx.role == Role.USER:
        proxied["user_id"] = ctx.user.user_id
    elif ctx.user.user_id and "user_id" not in proxied:
        proxied["user_id"] = ctx.user.user_id
    return proxied


@router.get("/health")
async def health_check(request: Request):
    """Health check endpoint for Bot API.

    Returns 503 if --with-bot is not enabled.
    Proxies to Vikingbot health check if enabled.
    """
    bot_url = get_bot_url()

    try:
        async with httpx.AsyncClient() as client:
            print(f"url={f'{bot_url}/bot/v1/health'}")
            # Forward to Vikingbot OpenAPIChannel health endpoint
            response = await client.get(
                f"{bot_url}/bot/v1/health",
                timeout=5.0,
            )
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
        logger.error(f"Failed to connect to bot service at {bot_url}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot service unavailable: {str(e)}",
        )
    except httpx.HTTPStatusError as e:
        logger.error(f"Bot service returned error: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot service error: {e.response.text}",
        )


@router.get("/images/{image_name:path}")
async def proxy_image(image_name: str, request: Request):
    """
    Proxy an image request to the actual Bot API.
    """
    bot_url = get_bot_url()

    import mimetypes
    media_type, _ = mimetypes.guess_type(image_name)
    media_type = media_type or "application/octet-stream"

    async def file_stream() -> AsyncGenerator[bytes, None]:
        try:
            async with httpx.AsyncClient() as client:
                async with client.stream(
                    "GET",
                    f"{bot_url}/bot/v1/images/{image_name}",
                    timeout=30.0,
                ) as response:
                    # If Bot API returns an error, we can't raise HTTPException anymore
                    # because the StreamingResponse has already started, but we can stop yielding.
                    if response.status_code != 200:
                        logger.error(f"Failed to fetch image from bot (status: {response.status_code})")
                        return
                    async for chunk in response.aiter_bytes():
                        yield chunk
        except Exception as e:
            logger.error(f"Error streaming image {image_name}: {e}")
            pass

    return StreamingResponse(
        file_stream(),
        media_type=media_type,
    )


@router.get("/resources/preview")
async def proxy_resource_preview(
    uri: str,
    request: Request,
    token: str | None = None,
    ctx: RequestContext = Depends(get_request_context),
):
    """Proxy a document preview, upgrading legacy unsigned history links when needed."""
    bot_url = get_bot_url()
    server_config = getattr(request.app.state, "config", None)
    preview_secret = (
        os.environ.get(RESOURCE_PREVIEW_SECRET_ENV, "").strip()
        or str(getattr(server_config, "root_api_key", "") or "").strip()
    )
    try:
        if token:
            claims = verify_resource_preview_token(token, secret=preview_secret, expected_uri=uri)
            if claims.account_id != ctx.account_id:
                logger.warning(
                    "Rejected resource preview for account mismatch: "
                    f"token_account={claims.account_id} request_account={ctx.account_id} uri={uri}"
                )
                raise HTTPException(
                    status_code=403,
                    detail="Resource preview belongs to another account",
                )
            preview_token = token
        else:
            preview_token = create_resource_preview_token(
                uri=uri,
                account_id=ctx.account_id,
                secret=preview_secret,
            )
    except ResourcePreviewTokenError:
        raise HTTPException(status_code=404, detail="Resource preview is not available")

    try:
        async with httpx.AsyncClient() as client:
            headers = {}
            accept = request.headers.get("accept")
            if accept:
                headers["Accept"] = accept
            response = await client.get(
                f"{bot_url}/bot/v1/resources/preview",
                params={"uri": uri, "token": preview_token},
                headers=headers,
                timeout=30.0,
            )
            response.raise_for_status()
            return StreamingResponse(
                iter([response.content]),
                media_type=response.headers.get("content-type", "text/html; charset=utf-8"),
            )
    except httpx.RequestError as e:
        logger.error(f"Failed to connect to bot service for resource preview: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot service unavailable: {str(e)}",
        )
    except httpx.HTTPStatusError as e:
        logger.error(f"Bot resource preview returned error: {e}")
        raise HTTPException(status_code=e.response.status_code, detail=e.response.text)


@router.post("/chat")
async def chat(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
):
    """Send a message to the bot and get a response.

    Proxies the request to Vikingbot OpenAPIChannel.
    """
    bot_url = get_bot_url()
    auth_token = extract_auth_token(request)

    # Read request body
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON in request body",
        )
    body = _prepare_bot_request_body(body, ctx)

    try:
        async with httpx.AsyncClient() as client:
            # Build headers - only include X-API-Key if provided
            headers = {"Content-Type": "application/json"}
            if auth_token:
                headers["X-API-Key"] = auth_token

            # Forward to Vikingbot OpenAPIChannel chat endpoint
            response = await client.post(
                f"{bot_url}/bot/v1/chat",
                json=body,
                headers=headers,
                timeout=300.0,  # 5 minute timeout for chat
            )
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
        logger.error(f"Failed to connect to bot service: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot service unavailable: {str(e)}",
        )
    except httpx.HTTPStatusError as e:
        logger.error(f"Bot service returned error: {e}")
        # Forward the status code if it's a client error
        if e.response.status_code < 500:
            raise HTTPException(
                status_code=e.response.status_code,
                detail=e.response.text,
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot service error: {e.response.text}",
        )


@router.post("/chat/stream")
async def chat_stream(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
):
    """Send a message to the bot and get a streaming response.

    Proxies the request to Vikingbot OpenAPIChannel with SSE streaming.
    """
    bot_url = get_bot_url()
    auth_token = extract_auth_token(request)

    # Read request body
    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON in request body",
        )
    body = _prepare_bot_request_body(body, ctx)

    async def event_stream() -> AsyncGenerator[str, None]:
        """Generate SSE events from bot response stream."""
        try:
            async with httpx.AsyncClient() as client:
                # Build headers - only include X-API-Key if provided
                headers = {"Content-Type": "application/json"}
                if auth_token:
                    headers["X-API-Key"] = auth_token

                # Forward to Vikingbot OpenAPIChannel stream endpoint
                async with client.stream(
                    "POST",
                    f"{bot_url}/bot/v1/chat/stream",
                    json=body,
                    headers=headers,
                    timeout=300.0,
                ) as response:
                    response.raise_for_status()

                    # Stream the response content
                    async for line in response.aiter_lines():
                        if line:
                            # Forward the SSE line as-is
                            yield f"{line}\n"
        except httpx.RequestError as e:
            logger.error(f"Failed to connect to bot service: {e}")
            error_event = {
                "event": "error",
                "data": json.dumps({"error": f"Bot service unavailable: {str(e)}"}),
            }
            yield f"data: {json.dumps(error_event)}\n\n"
        except httpx.HTTPStatusError as e:
            logger.error(f"Bot service returned error: {e}")
            error_event = {
                "event": "error",
                "data": json.dumps({"error": f"Bot service error: {e.response.text}"}),
            }
            yield f"data: {json.dumps(error_event)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


@router.post("/handoff")
async def handoff(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
):
    """Create a human handoff via the bot service."""
    bot_url = get_bot_url()
    auth_token = extract_auth_token(request)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON in request body",
        )
    body = _prepare_bot_request_body(body, ctx)

    try:
        async with httpx.AsyncClient() as client:
            headers = {"Content-Type": "application/json"}
            if auth_token:
                headers["X-API-Key"] = auth_token

            response = await client.post(
                f"{bot_url}/bot/v1/handoff",
                json=body,
                headers=headers,
                timeout=30.0,
            )
            response.raise_for_status()
            return response.json()
    except httpx.RequestError as e:
        logger.error(f"Failed to connect to bot service: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot service unavailable: {str(e)}",
        )
    except httpx.HTTPStatusError as e:
        logger.error(f"Bot service returned error: {e}")
        if e.response.status_code < 500:
            raise HTTPException(
                status_code=e.response.status_code,
                detail=e.response.text,
            )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Bot service error: {e.response.text}",
        )
