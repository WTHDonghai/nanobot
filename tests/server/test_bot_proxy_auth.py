# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Regression tests for bot proxy endpoint auth enforcement."""

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import openviking.server.routers.bot as bot_router_module


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
    assert captured["json"] == {"session_id": "session-1", "reason": "need human"}
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "X-API-Key": "test-key",
    }
    assert captured["timeout"] == 30.0
