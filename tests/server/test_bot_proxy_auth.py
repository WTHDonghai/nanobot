# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Regression tests for bot proxy endpoint auth enforcement."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import openviking.server.routers.bot as bot_router_module
from openviking.server.auth import get_request_context
from openviking.server.config import PublicBotConfig
from openviking.server.identity import RequestContext, Role
from openviking.server.public_bot import PublicBotIdentityResolver
from openviking_cli.resource_preview import (
    RESOURCE_PREVIEW_SECRET_ENV,
    create_resource_preview_token,
    verify_resource_preview_token,
)
from openviking_cli.session.user_id import UserIdentifier


def make_request(headers: dict[str, str]) -> Request:
    """Create a minimal request object with the provided headers."""
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [
                (key.lower().encode("latin-1"), value.encode("latin-1"))
                for key, value in headers.items()
            ],
            "query_string": b"",
        }
    )


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-API-Key": "test-key"}, "test-key"),
        ({"Authorization": "Bearer test-token"}, "test-token"),
    ],
)
def test_extract_auth_token(headers: dict[str, str], expected: str):
    """Accepted auth header formats should both produce a token."""
    assert bot_router_module.extract_auth_token(make_request(headers)) == expected


def test_public_bot_identity_allows_signed_resource_preview_path() -> None:
    resolver = object.__new__(PublicBotIdentityResolver)
    resolver.config = PublicBotConfig(enabled=True)

    assert resolver.is_enabled_for_path("/bot/v1/resources/preview") is True


def test_handoff_proxy_forwards_body_and_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class StubResponse:
        status_code = 200
        text = '{"success": true}'

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "success": True,
                "status": "accepted",
                "message": "ok",
                "entry_url": "https://example.com/handoff",
            }

    class StubAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json, headers, timeout):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            captured["timeout"] = timeout
            return StubResponse()

    monkeypatch.setattr(bot_router_module.httpx, "AsyncClient", StubAsyncClient)
    bot_router_module.set_bot_api_url("http://bot-service")

    app = FastAPI()
    app.include_router(bot_router_module.router, prefix="/bot/v1")

    client = TestClient(app)
    response = client.post(
        "/bot/v1/handoff",
        json={"session_id": "session-1", "reason": "need human"},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    assert response.json()["entry_url"] == "https://example.com/handoff"
    assert captured["url"] == "http://bot-service/bot/v1/handoff"
    assert captured["json"] == {
        "session_id": "session-1",
        "reason": "need human",
        "user_id": "default",
    }
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "X-API-Key": "test-key",
    }
    assert captured["timeout"] == 30.0


def test_resource_preview_proxy_forwards_accept_header(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class StubResponse:
        status_code = 200
        content = b'{"title":"doc","uri":"viking://resources/doc.md","markdown":"body"}'
        text = content.decode("utf-8")
        headers = {"content-type": "application/json"}

        def raise_for_status(self) -> None:
            return None

    class StubAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params, headers, timeout):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            captured["timeout"] = timeout
            return StubResponse()

    monkeypatch.setattr(bot_router_module.httpx, "AsyncClient", StubAsyncClient)
    monkeypatch.setenv(RESOURCE_PREVIEW_SECRET_ENV, "preview-secret")
    bot_router_module.set_bot_api_url("http://bot-service")

    async def user_context() -> RequestContext:
        return RequestContext(
            user=UserIdentifier("acme", "guest_123", "default"),
            role=Role.USER,
        )

    app = FastAPI()
    app.dependency_overrides[get_request_context] = user_context
    app.include_router(bot_router_module.router, prefix="/bot/v1")

    client = TestClient(app)
    uri = "viking://resources/doc.md"
    token = create_resource_preview_token(
        uri=uri,
        account_id="acme",
        secret="preview-secret",
    )
    response = client.get(
        "/bot/v1/resources/preview",
        params={"uri": uri, "token": token},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["markdown"] == "body"
    assert captured["url"] == "http://bot-service/bot/v1/resources/preview"
    assert captured["params"] == {"uri": uri, "token": token}
    assert captured["headers"] == {"Accept": "application/json"}
    assert captured["timeout"] == 30.0


def test_resource_preview_proxy_rejects_cross_account_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(RESOURCE_PREVIEW_SECRET_ENV, "preview-secret")
    bot_router_module.set_bot_api_url("http://bot-service")

    async def user_context() -> RequestContext:
        return RequestContext(
            user=UserIdentifier("acme", "guest_123", "default"),
            role=Role.USER,
        )

    app = FastAPI()
    app.dependency_overrides[get_request_context] = user_context
    app.include_router(bot_router_module.router, prefix="/bot/v1")
    uri = "viking://resources/doc.md"
    token = create_resource_preview_token(
        uri=uri,
        account_id="other-account",
        secret="preview-secret",
    )

    response = TestClient(app).get(
        "/bot/v1/resources/preview",
        params={"uri": uri, "token": token},
    )

    assert response.status_code == 403


def test_resource_preview_proxy_upgrades_legacy_unsigned_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class StubResponse:
        status_code = 200
        content = b'{"title":"doc","markdown":"body"}'
        text = content.decode("utf-8")
        headers = {"content-type": "application/json"}

        def raise_for_status(self) -> None:
            return None

    class StubAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, params, headers, timeout):
            captured["params"] = params
            return StubResponse()

    monkeypatch.setattr(bot_router_module.httpx, "AsyncClient", StubAsyncClient)
    monkeypatch.setenv(RESOURCE_PREVIEW_SECRET_ENV, "preview-secret")
    bot_router_module.set_bot_api_url("http://bot-service")

    async def user_context() -> RequestContext:
        return RequestContext(
            user=UserIdentifier("acme", "guest_123", "default"),
            role=Role.USER,
        )

    app = FastAPI()
    app.dependency_overrides[get_request_context] = user_context
    app.include_router(bot_router_module.router, prefix="/bot/v1")
    uri = "viking://resources/doc.md"

    response = TestClient(app).get(
        "/bot/v1/resources/preview",
        params={"uri": uri},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    forwarded = captured["params"]
    assert isinstance(forwarded, dict)
    claims = verify_resource_preview_token(
        forwarded["token"],
        secret="preview-secret",
        expected_uri=uri,
    )
    assert claims.account_id == "acme"


def test_chat_stream_proxy_overrides_user_id_for_user_role(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class StubStreamResponse:
        status_code = 200
        text = ""

        def raise_for_status(self) -> None:
            return None

        async def aiter_lines(self):
            yield 'data: {"event":"response","data":"ok"}'

    class StubAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def stream(self, method, url, json, headers, timeout):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            captured["timeout"] = timeout

            class _ContextManager:
                async def __aenter__(self_inner):
                    return StubStreamResponse()

                async def __aexit__(self_inner, exc_type, exc, tb):
                    return False

            return _ContextManager()

    async def user_ctx() -> RequestContext:
        return RequestContext(
            user=UserIdentifier("acme", "guest_123", "default"),
            role=Role.USER,
        )

    monkeypatch.setattr(bot_router_module.httpx, "AsyncClient", StubAsyncClient)
    bot_router_module.set_bot_api_url("http://bot-service")

    app = FastAPI()
    app.dependency_overrides[get_request_context] = user_ctx
    app.include_router(bot_router_module.router, prefix="/bot/v1")

    client = TestClient(app)
    response = client.post(
        "/bot/v1/chat/stream",
        json={"message": "hello", "user_id": "tampered-user"},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200
    assert captured["method"] == "POST"
    assert captured["url"] == "http://bot-service/bot/v1/chat/stream"
    assert captured["json"] == {"message": "hello", "user_id": "guest_123"}
