# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Tests for anonymous public bot identity resolution."""

import httpx
import pytest_asyncio

from openviking.server.api_keys import APIKeyManager
from openviking.server.app import create_app
from openviking.server.config import PublicBotConfig, ServerConfig
from openviking.server.dependencies import set_service
from openviking.server.public_bot import PublicBotIdentityResolver
from openviking.service.core import OpenVikingService
from openviking_cli.session.user_id import UserIdentifier

ROOT_KEY = "public-bot-root-key-for-testing-abcdef123456"


@pytest_asyncio.fixture(scope="function")
async def public_bot_service(temp_dir):
    svc = OpenVikingService(
        path=str(temp_dir / "public_bot_data"),
        user=UserIdentifier.the_default_user("public_bot_admin"),
    )
    await svc.initialize()
    yield svc
    await svc.close()


@pytest_asyncio.fixture(scope="function")
async def public_bot_app(public_bot_service):
    config = ServerConfig(
        root_api_key=ROOT_KEY,
        public_bot=PublicBotConfig(
            enabled=True,
            account_id="default",
            secret="public-bot-cookie-secret",
        ),
    )
    app = create_app(config=config, service=public_bot_service)
    set_service(public_bot_service)

    manager = APIKeyManager(root_key=ROOT_KEY, viking_fs=public_bot_service.viking_fs)
    await manager.load()
    app.state.api_key_manager = manager

    resolver = PublicBotIdentityResolver(
        config=config.public_bot,
        api_key_manager=manager,
        service=public_bot_service,
        root_api_key=ROOT_KEY,
    )
    await resolver.validate()
    app.state.public_bot_identity_resolver = resolver

    return app


@pytest_asyncio.fixture(scope="function")
async def public_bot_client(public_bot_app):
    transport = httpx.ASGITransport(app=public_bot_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def test_public_bot_whoami_sets_cookie_and_provisions_user(
    public_bot_app,
    public_bot_client: httpx.AsyncClient,
):
    resp = await public_bot_client.get("/api/v1/system/whoami")
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["role"] == "user"
    assert body["result"]["account_id"] == "default"
    assert body["result"]["user_id"].startswith("guest_")
    assert "ov_guest_id=" in resp.headers.get("set-cookie", "")

    users = public_bot_app.state.api_key_manager.get_users("default")
    assert any(user["user_id"] == body["result"]["user_id"] for user in users)

    resp2 = await public_bot_client.get("/api/v1/system/whoami")
    assert resp2.status_code == 200
    assert resp2.json()["result"]["user_id"] == body["result"]["user_id"]


async def test_public_bot_sessions_are_isolated_per_browser(public_bot_app):
    transport = httpx.ASGITransport(app=public_bot_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_a:
        whoami_a = await client_a.get("/api/v1/system/whoami")
        user_a = whoami_a.json()["result"]["user_id"]

        create_resp = await client_a.post("/api/v1/sessions", json={})
        assert create_resp.status_code == 200
        session_id = create_resp.json()["result"]["session_id"]

        list_resp = await client_a.get("/api/v1/sessions")
        assert list_resp.status_code == 200
        session_ids = {entry["session_id"] for entry in list_resp.json()["result"]}
        assert session_id in session_ids

    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_b:
        whoami_b = await client_b.get("/api/v1/system/whoami")
        user_b = whoami_b.json()["result"]["user_id"]
        assert user_b != user_a

        list_resp = await client_b.get("/api/v1/sessions")
        assert list_resp.status_code == 200
        session_ids = {entry["session_id"] for entry in list_resp.json()["result"]}
        assert session_id not in session_ids
