"""Helpers for resolving stable OpenViking bot identities."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

from openviking_cli.session.user_id import UserIdentifier

if TYPE_CHECKING:
    from vikingbot.config.schema import Config


DEFAULT_OPENVIKING_AGENT_ID = "default"
DEFAULT_OPENVIKING_USER_ID = "default"


@dataclass(frozen=True)
class AgentMemoryIdentity:
    """Stable owner used for reusable agent memory."""

    owner_user_id: str
    agent_id: str
    agent_space_name: str


def _clean_identifier(value: str | None, default: str) -> str:
    text = str(value or "").strip()
    return text or default


def _clean_agent_identifier(value: str | None, default: str) -> str:
    text = _clean_identifier(value, default)
    if "#" in text:
        text = text.split("#", 1)[0].strip()
    return text or default


def agent_space_name_for(user_id: str, agent_id: str) -> str:
    """Return OpenViking's deterministic agent-space name."""
    owner = _clean_identifier(user_id, DEFAULT_OPENVIKING_USER_ID)
    agent = _clean_agent_identifier(agent_id, DEFAULT_OPENVIKING_AGENT_ID)
    try:
        return UserIdentifier("default", owner, agent).agent_space_name()
    except Exception:
        return hashlib.md5(f"{owner}:{agent}".encode()).hexdigest()[:12]


def _load_config_if_needed(config: "Config | None") -> "Config":
    if config is not None:
        return config
    from vikingbot.config.loader import load_config

    return load_config()


def resolve_openviking_agent_id(config: "Config | None" = None) -> str:
    """Return the configured bot agent id, falling back to OpenViking default."""
    resolved_config = _load_config_if_needed(config)
    ov_server = getattr(resolved_config, "ov_server", None)
    return _clean_agent_identifier(
        getattr(ov_server, "agent_id", None),
        DEFAULT_OPENVIKING_AGENT_ID,
    )


def resolve_agent_memory_identity(config: "Config | None" = None) -> AgentMemoryIdentity:
    """Return the canonical OpenViking owner for reusable agent memory."""
    resolved_config = _load_config_if_needed(config)
    ov_server = getattr(resolved_config, "ov_server", None)
    owner_user_id = _clean_identifier(
        getattr(ov_server, "agent_memory_owner_user_id", None),
        _clean_identifier(
            getattr(ov_server, "admin_user_id", None),
            DEFAULT_OPENVIKING_USER_ID,
        ),
    )
    agent_id = resolve_openviking_agent_id(resolved_config)
    return AgentMemoryIdentity(
        owner_user_id=owner_user_id,
        agent_id=agent_id,
        agent_space_name=agent_space_name_for(owner_user_id, agent_id),
    )
