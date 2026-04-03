# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

"""Tests for explicit OpenViking memory scope policy in Vikingbot sessions."""

from vikingbot.agent.loop import AgentLoop
from vikingbot.bus.events import InboundMessage
from vikingbot.config.schema import Config, SessionKey
from vikingbot.session.manager import Session


def _make_loop(admin_user_id: str = "shared-owner") -> AgentLoop:
    loop = object.__new__(AgentLoop)
    loop.config = Config()
    loop.config.ov_server.admin_user_id = admin_user_id
    return loop


def _make_session() -> Session:
    return Session(key=SessionKey(type="cli", channel_id="default", chat_id="scope-test"))


def test_apply_openviking_memory_policy_keeps_direct_session_user_scoped() -> None:
    loop = _make_loop()
    session = _make_session()
    msg = InboundMessage(
        sender_id="alice",
        content="hello",
        session_key=session.key,
        metadata={},
    )

    loop._apply_openviking_memory_policy(session, msg)

    assert session.metadata["openviking_memory_scope"] == "all"
    assert session.metadata["openviking_user_id"] == "alice"
    assert "openviking_agent_owner_user_id" not in session.metadata


def test_apply_openviking_memory_policy_marks_group_chat_as_agent_scoped() -> None:
    loop = _make_loop(admin_user_id="group-owner")
    session = _make_session()
    msg = InboundMessage(
        sender_id="alice",
        content="group hello",
        session_key=session.key,
        metadata={"chat_type": "group"},
    )

    loop._apply_openviking_memory_policy(session, msg)

    assert session.metadata["openviking_memory_scope"] == "agent"
    assert session.metadata["openviking_agent_owner_user_id"] == "group-owner"
    assert "openviking_user_id" not in session.metadata


def test_apply_openviking_memory_policy_upgrades_multi_sender_session_to_agent_scope() -> None:
    loop = _make_loop(admin_user_id="shared-bot")
    session = _make_session()
    session.add_message("user", "first", sender_id="alice")
    session.metadata["openviking_memory_scope"] = "all"
    session.metadata["openviking_user_id"] = "alice"

    msg = InboundMessage(
        sender_id="bob",
        content="second",
        session_key=session.key,
        metadata={},
    )

    loop._apply_openviking_memory_policy(session, msg)

    assert session.metadata["openviking_memory_scope"] == "agent"
    assert session.metadata["openviking_agent_owner_user_id"] == "shared-bot"
    assert "openviking_user_id" not in session.metadata
