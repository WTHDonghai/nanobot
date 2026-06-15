# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Tests for Admin API endpoints (openviking/server/routers/admin.py)."""

import json
import uuid
from datetime import date, timedelta
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio

from openviking.server.api_keys import APIKeyManager
from openviking.server.app import create_app
from openviking.server.config import ServerConfig
from openviking.server.dependencies import set_service
from openviking.service.core import OpenVikingService
from openviking.service.session_service import MAX_ADMIN_DAILY_BUCKETS
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config.open_viking_config import OpenVikingConfigSingleton
from tests.utils.mock_agfs import MockLocalAGFS


def _uid() -> str:
    return f"acme_{uuid.uuid4().hex[:8]}"


ROOT_KEY = "admin-api-test-root-key-abcdef1234567890ab"


@pytest.fixture(autouse=True)
def _configure_test_env(monkeypatch, tmp_path):
    config_path = tmp_path / "ov.conf"
    config_path.write_text(
        json.dumps(
            {
                "storage": {
                    "workspace": str(tmp_path / "workspace"),
                    "agfs": {"backend": "local", "mode": "binding-client"},
                    "vectordb": {"backend": "local"},
                },
                "embedding": {
                    "dense": {
                        "provider": "openai",
                        "model": "test-embedder",
                        "api_base": "http://127.0.0.1:11434/v1",
                        "dimension": 1024,
                    }
                },
                "encryption": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )

    mock_agfs = MockLocalAGFS(root_path=tmp_path / "mock_agfs_root")

    monkeypatch.setenv("OPENVIKING_CONFIG_FILE", str(config_path))
    OpenVikingConfigSingleton.reset_instance()

    with patch("openviking.utils.agfs_utils.create_agfs_client", return_value=mock_agfs):
        yield

    OpenVikingConfigSingleton.reset_instance()


@pytest_asyncio.fixture(scope="function")
async def admin_service(temp_dir):
    svc = OpenVikingService(
        path=str(temp_dir / "admin_data"), user=UserIdentifier.the_default_user("admin_user")
    )
    await svc.initialize()
    yield svc
    await svc.close()


@pytest_asyncio.fixture(scope="function")
async def admin_app(admin_service):
    config = ServerConfig(root_api_key=ROOT_KEY)
    app = create_app(config=config, service=admin_service)
    set_service(admin_service)

    manager = APIKeyManager(root_key=ROOT_KEY, viking_fs=admin_service.viking_fs)
    await manager.load()
    app.state.api_key_manager = manager

    return app


@pytest_asyncio.fixture(scope="function")
async def admin_client(admin_app):
    transport = httpx.ASGITransport(app=admin_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def root_headers():
    return {"X-API-Key": ROOT_KEY}


# ---- Account CRUD ----


async def test_create_account(admin_client: httpx.AsyncClient):
    """ROOT can create an account with first admin."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["account_id"] == acct
    assert body["result"]["admin_user_id"] == "alice"
    assert "user_key" in body["result"]


async def test_list_accounts(admin_client: httpx.AsyncClient):
    """ROOT can list all accounts."""
    acct = _uid()
    await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    resp = await admin_client.get("/api/v1/admin/accounts", headers=root_headers())
    assert resp.status_code == 200
    accounts = resp.json()["result"]
    account_ids = {a["account_id"] for a in accounts}
    assert "default" in account_ids
    assert acct in account_ids


async def test_delete_account(admin_client: httpx.AsyncClient):
    """ROOT can delete an account."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    user_key = resp.json()["result"]["user_key"]

    resp = await admin_client.delete(f"/api/v1/admin/accounts/{acct}", headers=root_headers())
    assert resp.status_code == 200
    assert resp.json()["result"]["deleted"] is True

    # User key should now be invalid
    resp = await admin_client.get(
        "/api/v1/system/whoami",
        headers={"X-API-Key": user_key},
    )
    assert resp.status_code == 401


async def test_create_duplicate_account_fails(admin_client: httpx.AsyncClient):
    """Creating duplicate account should fail."""
    acct = _uid()
    await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "bob"},
        headers=root_headers(),
    )
    assert resp.status_code == 409  # ALREADY_EXISTS


# ---- User CRUD ----


async def test_register_user(admin_client: httpx.AsyncClient):
    """ROOT can register a user in an account."""
    acct = _uid()
    await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["user_id"] == "bob"
    assert "user_key" in body["result"]

    # Bob's key should work
    bob_key = body["result"]["user_key"]
    resp = await admin_client.get(
        "/api/v1/system/whoami",
        headers={"X-API-Key": bob_key},
    )
    assert resp.status_code == 200


async def test_admin_can_register_user_in_own_account(admin_client: httpx.AsyncClient):
    """ADMIN can register users in their own account."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]

    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers={"X-API-Key": alice_key},
    )
    assert resp.status_code == 200


async def test_admin_cannot_register_user_in_other_account(admin_client: httpx.AsyncClient):
    """ADMIN cannot register users in another account."""
    acct = _uid()
    other = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]

    await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": other, "admin_user_id": "eve"},
        headers=root_headers(),
    )

    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{other}/users",
        json={"user_id": "bob", "role": "user"},
        headers={"X-API-Key": alice_key},
    )
    assert resp.status_code == 403


async def test_list_users(admin_client: httpx.AsyncClient):
    """ROOT can list users in an account."""
    acct = _uid()
    await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    resp = await admin_client.get(f"/api/v1/admin/accounts/{acct}/users", headers=root_headers())
    assert resp.status_code == 200
    users = resp.json()["result"]
    user_ids = {u["user_id"] for u in users}
    assert user_ids == {"alice", "bob"}


async def test_remove_user(admin_client: httpx.AsyncClient):
    """ROOT can remove a user."""
    acct = _uid()
    await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    resp = await admin_client.delete(
        f"/api/v1/admin/accounts/{acct}/users/bob", headers=root_headers()
    )
    assert resp.status_code == 200

    # Bob's key should be invalid now
    resp = await admin_client.get(
        "/api/v1/system/whoami",
        headers={"X-API-Key": bob_key},
    )
    assert resp.status_code == 401


# ---- Role management ----


async def test_set_role(admin_client: httpx.AsyncClient):
    """ROOT can change a user's role."""
    acct = _uid()
    await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    resp = await admin_client.put(
        f"/api/v1/admin/accounts/{acct}/users/bob/role",
        json={"role": "admin"},
        headers=root_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["result"]["role"] == "admin"


async def test_regenerate_key(admin_client: httpx.AsyncClient):
    """ROOT can regenerate a user's key."""
    acct = _uid()
    await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    old_key = resp.json()["result"]["user_key"]

    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users/bob/key",
        headers=root_headers(),
    )
    assert resp.status_code == 200
    new_key = resp.json()["result"]["user_key"]
    assert new_key != old_key

    # Old key invalid
    resp = await admin_client.get(
        "/api/v1/system/whoami",
        headers={"X-API-Key": old_key},
    )
    assert resp.status_code == 401

    # New key valid
    resp = await admin_client.get(
        "/api/v1/system/whoami",
        headers={"X-API-Key": new_key},
    )
    assert resp.status_code == 200


# ---- Session audit and analytics ----


async def test_admin_session_audit_and_daily_analytics(admin_client: httpx.AsyncClient):
    """ADMIN can inspect user sessions and daily quality metrics in own account."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
    assert create_resp.status_code == 200
    session_id = create_resp.json()["result"]["session_id"]
    await admin_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"role": "user", "content": "How do I reset the headset?"},
        headers={"X-API-Key": bob_key},
    )
    await admin_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "role": "assistant",
            "content": "Hold the power button for ten seconds.",
            "token_usage": {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "total_tokens": 20,
            },
        },
        headers={"X-API-Key": bob_key},
    )

    list_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&q=headset",
        headers={"X-API-Key": alice_key},
    )
    assert list_resp.status_code == 200
    session_page = list_resp.json()["result"]
    sessions = session_page["items"]
    assert session_page["total"] == 1
    assert session_page["page"] == 1
    assert session_page["page_size"] == 50
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == session_id
    assert sessions[0]["user_message_count"] == 1
    assert sessions[0]["assistant_message_count"] == 1
    assert sessions[0]["token_usage"]["total_tokens"] >= 20

    detail_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions/{session_id}?user_id=bob",
        headers={"X-API-Key": alice_key},
    )
    assert detail_resp.status_code == 200
    detail = detail_resp.json()["result"]
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["token_usage"]["total_tokens"] == 20

    analytics_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/analytics/daily?user_id=bob",
        headers={"X-API-Key": alice_key},
    )
    assert analytics_resp.status_code == 200
    totals = analytics_resp.json()["result"]["totals"]
    assert totals["active_users"] == 1
    assert totals["session_count"] == 1
    assert totals["message_count"] == 2
    assert totals["token_usage"]["total_tokens"] >= 20


async def test_admin_session_audit_lists_all_users_in_account(admin_client: httpx.AsyncClient):
    """ADMIN can aggregate sessions across every user in their own account."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    created_sessions = []
    for user_id in ["bob", "cindy"]:
        resp = await admin_client.post(
            f"/api/v1/admin/accounts/{acct}/users",
            json={"user_id": user_id, "role": "user"},
            headers=root_headers(),
        )
        user_key = resp.json()["result"]["user_key"]
        create_resp = await admin_client.post(
            "/api/v1/sessions",
            headers={"X-API-Key": user_key},
        )
        session_id = create_resp.json()["result"]["session_id"]
        created_sessions.append((user_id, session_id))
        await admin_client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"role": "user", "content": f"Question from {user_id}"},
            headers={"X-API-Key": user_key},
        )

    list_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions",
        headers={"X-API-Key": alice_key},
    )
    assert list_resp.status_code == 200
    session_page = list_resp.json()["result"]
    assert session_page["total"] >= 2
    session_keys = {
        (session["user_id"], session["session_id"])
        for session in session_page["items"]
    }
    assert session_keys.issuperset(set(created_sessions))

    analytics_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/analytics/daily",
        headers={"X-API-Key": alice_key},
    )
    assert analytics_resp.status_code == 200
    totals = analytics_resp.json()["result"]["totals"]
    assert totals["active_users"] == 2
    assert totals["session_count"] == 2


async def test_admin_session_audit_paginates_sessions(admin_client: httpx.AsyncClient):
    """ADMIN can page through session audit results."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    created_session_ids = []
    for index in range(3):
        create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
        session_id = create_resp.json()["result"]["session_id"]
        created_session_ids.append(session_id)
        await admin_client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"role": "user", "content": f"page me {index}"},
            headers={"X-API-Key": bob_key},
        )

    first_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&page=1&page_size=2",
        headers={"X-API-Key": alice_key},
    )
    assert first_resp.status_code == 200
    first_page = first_resp.json()["result"]
    assert first_page["page"] == 1
    assert first_page["page_size"] == 2
    assert first_page["total"] == 3
    assert first_page["total_pages"] == 2
    assert first_page["has_prev"] is False
    assert first_page["has_next"] is True
    assert len(first_page["items"]) == 2

    second_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&page=2&page_size=2",
        headers={"X-API-Key": alice_key},
    )
    assert second_resp.status_code == 200
    second_page = second_resp.json()["result"]
    assert second_page["page"] == 2
    assert second_page["total"] == 3
    assert second_page["total_pages"] == 2
    assert second_page["has_prev"] is True
    assert second_page["has_next"] is False
    assert len(second_page["items"]) == 1
    returned_ids = {item["session_id"] for item in first_page["items"] + second_page["items"]}
    assert returned_ids == set(created_session_ids)


async def test_admin_session_audit_sorts_by_value_fields(admin_client: httpx.AsyncClient):
    """ADMIN can sort audit sessions by useful operational metrics."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    specs = [
        ("low", 8, "completed"),
        ("high", 80, "completed"),
        ("failed", 20, "error"),
    ]
    session_ids = {}
    for label, total_tokens, tool_status in specs:
        create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
        session_id = create_resp.json()["result"]["session_id"]
        session_ids[label] = session_id
        await admin_client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "role": "assistant",
                "token_usage": {
                    "prompt_tokens": total_tokens // 2,
                    "completion_tokens": total_tokens - total_tokens // 2,
                    "total_tokens": total_tokens,
                },
                "parts": [
                    {"type": "text", "text": label},
                    {
                        "type": "tool",
                        "tool_id": f"tool_{label}",
                        "tool_name": "lookup",
                        "tool_status": tool_status,
                    },
                ],
            },
            headers={"X-API-Key": bob_key},
        )

    token_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&sort_by=token_total&sort_order=desc",
        headers={"X-API-Key": alice_key},
    )
    assert token_resp.status_code == 200
    token_page = token_resp.json()["result"]
    assert token_page["sort_by"] == "token_total"
    assert token_page["sort_order"] == "desc"
    assert [item["session_id"] for item in token_page["items"]] == [
        session_ids["high"],
        session_ids["failed"],
        session_ids["low"],
    ]

    failure_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&sort_by=failed_tool_call_count&sort_order=desc",
        headers={"X-API-Key": alice_key},
    )
    assert failure_resp.status_code == 200
    failure_items = failure_resp.json()["result"]["items"]
    assert failure_items[0]["session_id"] == session_ids["failed"]
    assert failure_items[0]["failed_tool_call_count"] == 1


async def test_admin_session_audit_clamps_page_past_end(admin_client: httpx.AsyncClient):
    """Page requests past the end return the last valid page."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    for index in range(3):
        create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
        session_id = create_resp.json()["result"]["session_id"]
        await admin_client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"role": "user", "content": f"clamp page {index}"},
            headers={"X-API-Key": bob_key},
        )

    resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&page=99&page_size=2",
        headers={"X-API-Key": alice_key},
    )

    assert resp.status_code == 200
    page = resp.json()["result"]
    assert page["page"] == 2
    assert page["page_size"] == 2
    assert page["total"] == 3
    assert page["total_pages"] == 2
    assert page["has_prev"] is True
    assert page["has_next"] is False
    assert len(page["items"]) == 1


async def test_admin_session_audit_pagination_uses_stable_tie_breakers(
    admin_client: httpx.AsyncClient,
):
    """Sessions with identical timestamps keep a deterministic order across pages."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    for index in range(4):
        create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
        session_id = create_resp.json()["result"]["session_id"]
        await admin_client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={
                "role": "user",
                "content": f"same time page {index}",
                "created_at": "2026-06-12T10:00:00+00:00",
            },
            headers={"X-API-Key": bob_key},
        )

    first_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&page=1&page_size=2",
        headers={"X-API-Key": alice_key},
    )
    second_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&page=2&page_size=2",
        headers={"X-API-Key": alice_key},
    )
    repeat_first_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&page=1&page_size=2",
        headers={"X-API-Key": alice_key},
    )

    assert first_resp.status_code == 200
    assert second_resp.status_code == 200
    assert repeat_first_resp.status_code == 200
    first_ids = [item["session_id"] for item in first_resp.json()["result"]["items"]]
    second_ids = [item["session_id"] for item in second_resp.json()["result"]["items"]]
    repeat_first_ids = [
        item["session_id"] for item in repeat_first_resp.json()["result"]["items"]
    ]
    assert first_ids == repeat_first_ids
    assert len(first_ids) == 2
    assert len(second_ids) == 2
    assert not set(first_ids).intersection(second_ids)
    assert first_ids == sorted(first_ids)
    assert second_ids == sorted(second_ids)


async def test_admin_session_audit_orders_same_instant_with_timezone_offsets(
    admin_client: httpx.AsyncClient,
):
    """Timezone offsets are normalized before session sort keys are persisted."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    session_times = {
        "later": "2026-06-12T18:30:00+08:00",
        "earlier_same_offset": "2026-06-12T10:00:00+00:00",
    }
    session_ids = {}
    for label, created_at in session_times.items():
        create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
        session_id = create_resp.json()["result"]["session_id"]
        session_ids[label] = session_id
        await admin_client.post(
            f"/api/v1/sessions/{session_id}/messages",
            json={"role": "user", "content": label, "created_at": created_at},
            headers={"X-API-Key": bob_key},
        )

    resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&page=1&page_size=2",
        headers={"X-API-Key": alice_key},
    )

    assert resp.status_code == 200
    items = resp.json()["result"]["items"]
    assert [item["session_id"] for item in items] == [
        session_ids["later"],
        session_ids["earlier_same_offset"],
    ]


async def test_admin_session_audit_uses_requested_timezone_for_dates(
    admin_client: httpx.AsyncClient,
):
    """Date filters and daily buckets use the requested admin timezone."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
    session_id = create_resp.json()["result"]["session_id"]
    await admin_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "role": "user",
            "content": "local early morning",
            "created_at": "2026-06-13T18:21:54+00:00",
        },
        headers={"X-API-Key": bob_key},
    )

    list_resp = await admin_client.get(
        (
            f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob"
            "&from_date=2026-06-14&to_date=2026-06-14&tz=Asia/Shanghai"
        ),
        headers={"X-API-Key": alice_key},
    )
    analytics_resp = await admin_client.get(
        (
            f"/api/v1/admin/accounts/{acct}/analytics/daily?user_id=bob"
            "&from_date=2026-06-12&to_date=2026-06-14&tz=Asia/Shanghai"
        ),
        headers={"X-API-Key": alice_key},
    )

    assert list_resp.status_code == 200
    page = list_resp.json()["result"]
    assert page["total"] == 1
    assert page["items"][0]["session_id"] == session_id

    assert analytics_resp.status_code == 200
    result = analytics_resp.json()["result"]
    assert result["timezone"] == "Asia/Shanghai"
    assert [row["date"] for row in result["daily"]] == [
        "2026-06-12",
        "2026-06-13",
        "2026-06-14",
    ]
    assert [row["session_count"] for row in result["daily"]] == [0, 0, 1]

    utc_analytics_resp = await admin_client.get(
        (
            f"/api/v1/admin/accounts/{acct}/analytics/daily?user_id=bob"
            "&from_date=2026-06-13&to_date=2026-06-13"
        ),
        headers={"X-API-Key": alice_key},
    )

    assert utc_analytics_resp.status_code == 200
    utc_result = utc_analytics_resp.json()["result"]
    assert [row["date"] for row in utc_result["daily"]] == ["2026-06-13"]
    assert utc_result["daily"][0]["session_count"] == 1

    precise_utc_resp = await admin_client.get(
        (
            f"/api/v1/admin/accounts/{acct}/analytics/daily?user_id=bob"
            "&from_date=2026-06-13T00:00:00%2B00:00"
            "&to_date=2026-06-13T18:00:00%2B00:00"
        ),
        headers={"X-API-Key": alice_key},
    )

    assert precise_utc_resp.status_code == 200
    precise_result = precise_utc_resp.json()["result"]
    assert [row["date"] for row in precise_result["daily"]] == ["2026-06-13"]
    assert precise_result["daily"][0]["session_count"] == 0


async def test_admin_session_audit_skips_gap_fill_for_large_daily_ranges(
    admin_client: httpx.AsyncClient,
):
    """Very wide analytics ranges avoid generating excessive empty buckets."""
    start_date = date(2026, 1, 1)
    end_date = start_date + timedelta(days=MAX_ADMIN_DAILY_BUCKETS)
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]

    resp = await admin_client.get(
        (
            f"/api/v1/admin/accounts/{acct}/analytics/daily"
            f"?from_date={start_date.isoformat()}&to_date={end_date.isoformat()}"
        ),
        headers={"X-API-Key": alice_key},
    )

    assert resp.status_code == 200
    assert resp.json()["result"]["daily"] == []


async def test_admin_session_audit_plain_list_uses_meta_summary(
    admin_client: httpx.AsyncClient,
    admin_service: OpenVikingService,
):
    """Plain paginated lists avoid reading raw messages for every session."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
    session_id = create_resp.json()["result"]["session_id"]
    await admin_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"role": "user", "content": "plain list should use meta"},
        headers={"X-API-Key": bob_key},
    )

    real_read_file = admin_service.sessions._viking_fs.read_file
    read_paths = []

    async def spy_read_file(uri, *args, **kwargs):
        read_paths.append(uri)
        return await real_read_file(uri, *args, **kwargs)

    with patch.object(admin_service.sessions._viking_fs, "read_file", side_effect=spy_read_file):
        resp = await admin_client.get(
            f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&page=1&page_size=20",
            headers={"X-API-Key": alice_key},
        )

    assert resp.status_code == 200
    page = resp.json()["result"]
    assert page["total"] == 1
    assert page["items"][0]["session_id"] == session_id
    assert page["items"][0]["message_count"] == 1
    assert any(path.endswith(".meta.json") for path in read_paths)
    assert not any(path.endswith("messages.jsonl") for path in read_paths), read_paths


async def test_admin_daily_analytics_uses_meta_summary(
    admin_client: httpx.AsyncClient,
    admin_service: OpenVikingService,
):
    """Daily analytics should not force raw message reads when summaries exist."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
    session_id = create_resp.json()["result"]["session_id"]
    await admin_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"role": "user", "content": "analytics should use meta"},
        headers={"X-API-Key": bob_key},
    )

    real_read_file = admin_service.sessions._viking_fs.read_file
    read_paths = []

    async def spy_read_file(uri, *args, **kwargs):
        read_paths.append(uri)
        return await real_read_file(uri, *args, **kwargs)

    with patch.object(admin_service.sessions._viking_fs, "read_file", side_effect=spy_read_file):
        resp = await admin_client.get(
            f"/api/v1/admin/accounts/{acct}/analytics/daily?user_id=bob",
            headers={"X-API-Key": alice_key},
        )

    assert resp.status_code == 200
    totals = resp.json()["result"]["totals"]
    assert totals["session_count"] == 1
    assert totals["message_count"] == 1
    assert any(path.endswith(".meta.json") for path in read_paths)
    assert not any(path.endswith("messages.jsonl") for path in read_paths), read_paths


async def test_admin_session_audit_backfills_legacy_meta_summary(
    admin_client: httpx.AsyncClient,
    admin_service: OpenVikingService,
):
    """Legacy sessions without cached audit summaries stay visible and get upgraded."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
    session_id = create_resp.json()["result"]["session_id"]
    await admin_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"role": "user", "content": "legacy summary should still list"},
        headers={"X-API-Key": bob_key},
    )

    bob_ctx = admin_service.sessions._user_ctx(acct, "bob")
    meta_uri = f"viking://session/bob/{session_id}/.meta.json"

    async def strip_audit_summary():
        meta = json.loads(await admin_service.sessions._viking_fs.read_file(meta_uri, ctx=bob_ctx))
        meta.pop("audit_summary", None)
        meta.pop("audit_summary_version", None)
        meta.pop("audit_summary_complete", None)
        await admin_service.sessions._viking_fs.write_file(
            meta_uri,
            json.dumps(meta),
            ctx=bob_ctx,
        )

    await strip_audit_summary()
    list_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob",
        headers={"X-API-Key": alice_key},
    )

    assert list_resp.status_code == 200
    page = list_resp.json()["result"]
    assert page["total"] == 1
    assert page["items"][0]["session_id"] == session_id
    assert page["items"][0]["message_count"] == 1
    assert page["items"][0]["user_message_count"] == 1
    meta = json.loads(await admin_service.sessions._viking_fs.read_file(meta_uri, ctx=bob_ctx))
    assert meta["audit_summary_version"] == 1
    assert meta["audit_summary"]["message_count"] == 1

    await strip_audit_summary()
    analytics_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/analytics/daily?user_id=bob",
        headers={"X-API-Key": alice_key},
    )

    assert analytics_resp.status_code == 200
    totals = analytics_resp.json()["result"]["totals"]
    assert totals["session_count"] == 1
    assert totals["message_count"] == 1
    meta = json.loads(await admin_service.sessions._viking_fs.read_file(meta_uri, ctx=bob_ctx))
    assert meta["audit_summary_version"] == 1
    assert meta["audit_summary"]["message_count"] == 1


async def test_admin_session_audit_search_reads_raw_messages(
    admin_client: httpx.AsyncClient,
    admin_service: OpenVikingService,
):
    """Full-text session search still inspects raw session records."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
    session_id = create_resp.json()["result"]["session_id"]
    await admin_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"role": "user", "content": "needle in raw message"},
        headers={"X-API-Key": bob_key},
    )

    real_read_file = admin_service.sessions._viking_fs.read_file
    read_paths = []

    async def spy_read_file(uri, *args, **kwargs):
        read_paths.append(uri)
        return await real_read_file(uri, *args, **kwargs)

    with patch.object(admin_service.sessions._viking_fs, "read_file", side_effect=spy_read_file):
        resp = await admin_client.get(
            f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&q=needle",
            headers={"X-API-Key": alice_key},
        )

    assert resp.status_code == 200
    page = resp.json()["result"]
    assert page["total"] == 1
    assert page["items"][0]["session_id"] == session_id
    assert any(path.endswith("messages.jsonl") for path in read_paths)


async def test_admin_session_audit_search_matches_later_text_part(
    admin_client: httpx.AsyncClient,
):
    """Full-text session search checks every text part, not only message.content."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
    session_id = create_resp.json()["result"]["session_id"]
    await admin_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "role": "assistant",
            "parts": [
                {"type": "text", "text": "first visible chunk"},
                {"type": "text", "text": "second hidden-needle chunk"},
            ],
        },
        headers={"X-API-Key": bob_key},
    )

    resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&q=hidden-needle",
        headers={"X-API-Key": alice_key},
    )

    assert resp.status_code == 200
    page = resp.json()["result"]
    assert page["total"] == 1
    assert page["items"][0]["session_id"] == session_id
    assert page["items"][0]["matched_message_count"] == 1


async def test_admin_session_detail_reads_archives_without_commit_count(
    admin_client: httpx.AsyncClient,
    admin_service: OpenVikingService,
):
    """Raw detail includes archived messages even when meta commit_count is stale."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
    session_id = create_resp.json()["result"]["session_id"]
    await admin_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"role": "user", "content": "archived question"},
        headers={"X-API-Key": bob_key},
    )
    commit_resp = await admin_client.post(
        f"/api/v1/sessions/{session_id}/commit",
        headers={"X-API-Key": bob_key},
    )
    assert commit_resp.status_code == 200

    bob_ctx = admin_service.sessions._user_ctx(acct, "bob")
    meta_uri = f"viking://session/bob/{session_id}/.meta.json"
    meta = json.loads(await admin_service.sessions._viking_fs.read_file(meta_uri, ctx=bob_ctx))
    meta["commit_count"] = 0
    await admin_service.sessions._viking_fs.write_file(
        meta_uri,
        json.dumps(meta),
        ctx=bob_ctx,
    )

    resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions/{session_id}?user_id=bob",
        headers={"X-API-Key": alice_key},
    )

    assert resp.status_code == 200
    detail = resp.json()["result"]
    assert detail["message_count"] == 1
    assert [message["parts"][0]["text"] for message in detail["messages"]] == [
        "archived question"
    ]


async def test_admin_session_audit_rejects_invalid_user_id(
    admin_client: httpx.AsyncClient,
):
    """Admin session endpoints validate user_id before building storage contexts."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]

    list_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob@example.com",
        headers={"X-API-Key": alice_key},
    )
    detail_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions/session-1?user_id=bob@example.com",
        headers={"X-API-Key": alice_key},
    )
    delete_resp = await admin_client.delete(
        f"/api/v1/admin/accounts/{acct}/sessions/session-1?user_id=bob@example.com",
        headers={"X-API-Key": alice_key},
    )

    assert list_resp.status_code == 422
    assert detail_resp.status_code == 422
    assert delete_resp.status_code == 422


async def test_admin_session_audit_rejects_invalid_date(
    admin_client: httpx.AsyncClient,
):
    """Invalid date filters return 422 instead of bubbling ValueError."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]

    session_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?from_date=not-a-date",
        headers={"X-API-Key": alice_key},
    )
    analytics_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/analytics/daily?to_date=not-a-date",
        headers={"X-API-Key": alice_key},
    )
    timezone_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/analytics/daily?tz=Not/AZone",
        headers={"X-API-Key": alice_key},
    )

    assert session_resp.status_code == 422
    assert analytics_resp.status_code == 422
    assert timezone_resp.status_code == 422


async def test_admin_session_audit_counts_failed_tool_status(
    admin_client: httpx.AsyncClient,
):
    """Both failed and error tool statuses count as failures."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
    session_id = create_resp.json()["result"]["session_id"]
    await admin_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "role": "assistant",
            "parts": [
                {
                    "type": "tool",
                    "tool_id": "tool_1",
                    "tool_name": "lookup",
                    "tool_status": "failed",
                    "tool_output": "boom",
                }
            ],
        },
        headers={"X-API-Key": bob_key},
    )

    resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob",
        headers={"X-API-Key": alice_key},
    )

    assert resp.status_code == 200
    item = resp.json()["result"]["items"][0]
    assert item["tool_call_count"] == 1
    assert item["failed_tool_call_count"] == 1


async def test_admin_session_audit_token_total_includes_tool_usage(
    admin_client: httpx.AsyncClient,
):
    """Message-level totals do not hide additional tool token costs."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
    session_id = create_resp.json()["result"]["session_id"]
    await admin_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "role": "assistant",
            "token_usage": {
                "prompt_tokens": 12,
                "completion_tokens": 8,
                "total_tokens": 20,
            },
            "parts": [
                {"type": "text", "text": "Here is the result."},
                {
                    "type": "tool",
                    "tool_id": "tool_1",
                    "tool_name": "lookup",
                    "tool_status": "completed",
                    "prompt_tokens": 5,
                    "completion_tokens": 7,
                },
            ],
        },
        headers={"X-API-Key": bob_key},
    )

    resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob",
        headers={"X-API-Key": alice_key},
    )

    assert resp.status_code == 200
    usage = resp.json()["result"]["items"][0]["token_usage"]
    assert usage["prompt_tokens"] == 17
    assert usage["completion_tokens"] == 15
    assert usage["total_tokens"] == 32


async def test_admin_can_delete_user_session_from_audit(
    admin_client: httpx.AsyncClient,
):
    """ADMIN can delete a target user's session through the audit endpoint."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
    session_id = create_resp.json()["result"]["session_id"]
    await admin_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={"role": "user", "content": "delete me"},
        headers={"X-API-Key": bob_key},
    )

    delete_resp = await admin_client.delete(
        f"/api/v1/admin/accounts/{acct}/sessions/{session_id}?user_id=bob",
        headers={"X-API-Key": alice_key},
    )

    assert delete_resp.status_code == 200
    get_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions/{session_id}?user_id=bob",
        headers={"X-API-Key": alice_key},
    )
    assert get_resp.status_code == 404


async def test_admin_cannot_delete_other_account_session_from_audit(
    admin_client: httpx.AsyncClient,
):
    """ADMIN cannot delete sessions in a different account."""
    acct = _uid()
    other = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": other, "admin_user_id": "eve"},
        headers=root_headers(),
    )
    eve_key = resp.json()["result"]["user_key"]
    create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": eve_key})
    session_id = create_resp.json()["result"]["session_id"]

    resp = await admin_client.delete(
        f"/api/v1/admin/accounts/{other}/sessions/{session_id}?user_id=eve",
        headers={"X-API-Key": alice_key},
    )

    assert resp.status_code == 403


async def test_admin_session_audit_handles_naive_message_time(
    admin_client: httpx.AsyncClient,
):
    """Timezone-naive message timestamps remain readable."""
    acct = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]
    create_resp = await admin_client.post("/api/v1/sessions", headers={"X-API-Key": bob_key})
    session_id = create_resp.json()["result"]["session_id"]

    add_resp = await admin_client.post(
        f"/api/v1/sessions/{session_id}/messages",
        json={
            "role": "user",
            "content": "naive time",
            "created_at": "2026-06-12T10:00:00",
        },
        headers={"X-API-Key": bob_key},
    )
    assert add_resp.status_code == 200

    list_resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions?user_id=bob&from_date=2026-06-12&to_date=2026-06-12",
        headers={"X-API-Key": alice_key},
    )
    assert list_resp.status_code == 200
    sessions = list_resp.json()["result"]["items"]
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == session_id


async def test_user_cannot_access_admin_session_audit(admin_client: httpx.AsyncClient):
    """Regular users cannot call admin audit endpoints."""
    acct = _uid()
    await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    resp = await admin_client.get(
        f"/api/v1/admin/accounts/{acct}/sessions",
        headers={"X-API-Key": bob_key},
    )
    assert resp.status_code == 403


async def test_admin_cannot_access_other_account_session_audit(
    admin_client: httpx.AsyncClient,
):
    """ADMIN cannot inspect sessions in a different account."""
    acct = _uid()
    other = _uid()
    resp = await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    alice_key = resp.json()["result"]["user_key"]
    await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": other, "admin_user_id": "eve"},
        headers=root_headers(),
    )

    resp = await admin_client.get(
        f"/api/v1/admin/accounts/{other}/sessions",
        headers={"X-API-Key": alice_key},
    )
    assert resp.status_code == 403


# ---- Permission guard ----


async def test_user_role_cannot_access_admin_api(admin_client: httpx.AsyncClient):
    """USER role should not access admin endpoints."""
    acct = _uid()
    await admin_client.post(
        "/api/v1/admin/accounts",
        json={"account_id": acct, "admin_user_id": "alice"},
        headers=root_headers(),
    )
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "bob", "role": "user"},
        headers=root_headers(),
    )
    bob_key = resp.json()["result"]["user_key"]

    # USER cannot register users
    resp = await admin_client.post(
        f"/api/v1/admin/accounts/{acct}/users",
        json={"user_id": "charlie", "role": "user"},
        headers={"X-API-Key": bob_key},
    )
    assert resp.status_code == 403


async def test_no_auth_admin_api_returns_401(admin_client: httpx.AsyncClient):
    """Admin API without key should return 401."""
    resp = await admin_client.get("/api/v1/admin/accounts")
    assert resp.status_code == 401
