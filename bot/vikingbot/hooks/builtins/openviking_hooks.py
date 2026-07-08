import re
from typing import Any

from loguru import logger

from vikingbot.config.loader import load_config
from vikingbot.config.schema import SessionKey
from vikingbot.openviking_identity import (
    resolve_agent_memory_identity,
    resolve_openviking_agent_id,
)

from ...session import Session
from ..base import Hook, HookContext

try:
    import openviking as ov
    from vikingbot.openviking_mount.ov_server import VikingClient

    HAS_OPENVIKING = True
except Exception:
    HAS_OPENVIKING = False
    VikingClient = None
    ov = None

# Global singleton client
_global_client: VikingClient | None = None

ALL_MEMORY_SCOPE = "all"
USER_MEMORY_SCOPE = "user"
AGENT_MEMORY_SCOPE = "agent"


async def get_global_client() -> VikingClient:
    """Get or create the global singleton VikingClient."""
    global _global_client
    if _global_client is None:
        _global_client = await VikingClient.create(resolve_openviking_agent_id())
    return _global_client


def resolve_openviking_user_id(
    metadata: dict[str, Any] | None = None,
) -> str:
    """Resolve the explicit user id that should own an OpenViking session write."""
    if metadata:
        user_id = metadata.get("openviking_user_id")
        if isinstance(user_id, str) and user_id.strip():
            return user_id.strip()
    raise ValueError("Missing explicit openviking_user_id for OpenViking sync")


def resolve_openviking_agent_owner_user_id(metadata: dict[str, Any] | None = None) -> str:
    """Resolve the stable user id that owns shared agent-space extractions."""
    if metadata:
        user_id = metadata.get("openviking_agent_owner_user_id")
        if isinstance(user_id, str) and user_id.strip():
            return user_id.strip()
    raise ValueError(
        "Missing explicit openviking_agent_owner_user_id for agent-scoped OpenViking sync"
    )


def resolve_openviking_memory_scope(metadata: dict[str, Any] | None = None) -> str:
    """Resolve memory extraction scope for a bot session."""
    if not metadata:
        return ALL_MEMORY_SCOPE

    scope = metadata.get("openviking_memory_scope")
    if scope is None:
        return ALL_MEMORY_SCOPE
    if isinstance(scope, str):
        normalized = scope.strip().lower()
        if normalized in {ALL_MEMORY_SCOPE, USER_MEMORY_SCOPE, AGENT_MEMORY_SCOPE}:
            return normalized
    raise ValueError(f"Invalid openviking_memory_scope: {scope}")


def resolve_openviking_session_id(
    session_key: SessionKey,
    messages: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Resolve the backing OpenViking session id for a bot session."""
    if metadata:
        session_id = metadata.get("openviking_session_id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()

    for message in reversed(messages or []):
        session_id = message.get("openviking_session_id")
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()

    return session_key.safe_name()


async def mirror_messages_to_openviking(
    session_key: SessionKey,
    messages: list[dict[str, Any]],
    *,
    metadata: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Write new session messages into OpenViking without committing them."""
    if not HAS_OPENVIKING:
        return {"success": False, "error": "OpenViking unavailable", "appended_indices": []}

    if not messages:
        return {"success": True, "appended_indices": []}

    memory_scope = resolve_openviking_memory_scope(metadata)
    if memory_scope != ALL_MEMORY_SCOPE:
        return {
            "success": False,
            "error": (
                "OpenViking live session mirroring only supports memory_scope='all'. "
                f"Received memory_scope='{memory_scope}'."
            ),
            "appended_indices": [],
        }

    session_id = resolve_openviking_session_id(session_key, messages=messages, metadata=metadata)
    try:
        resolved_user_id = (
            user_id.strip()
            if isinstance(user_id, str) and user_id.strip()
            else resolve_openviking_user_id(metadata=metadata)
        )
    except ValueError as e:
        return {"success": False, "error": str(e), "appended_indices": []}

    try:
        client = await get_global_client()
        return await client.append_messages(session_id, messages, resolved_user_id)
    except Exception as e:
        logger.exception(f"Failed to mirror session messages to OpenViking: {e}")
        return {"success": False, "error": str(e), "appended_indices": []}


class OpenVikingCompactHook(Hook):
    name = "openviking_compact"

    async def _get_client(self, workspace_id: str) -> VikingClient:
        # Use global singleton client
        return await get_global_client()

    async def execute(self, context: HookContext, **kwargs) -> Any:
        vikingbot_session: Session = kwargs.get("session", {})
        session_id = resolve_openviking_session_id(
            context.session_key,
            messages=vikingbot_session.messages,
            metadata=context.metadata,
        )
        memory_scope = resolve_openviking_memory_scope(context.metadata)

        try:
            client = await self._get_client(context.workspace_id)
            if memory_scope == AGENT_MEMORY_SCOPE:
                owner_user_id = resolve_openviking_agent_owner_user_id(context.metadata)
                result = await client.commit(
                    session_id,
                    vikingbot_session.messages,
                    owner_user_id,
                    memory_scope=AGENT_MEMORY_SCOPE,
                )
            else:
                user_id = resolve_openviking_user_id(metadata=context.metadata)
                result = await client.commit(
                    session_id,
                    vikingbot_session.messages,
                    user_id,
                    memory_scope=memory_scope,
                )
            return result
        except Exception as e:
            logger.exception(f"Failed to add message to OpenViking: {e}")
            return {"success": False, "error": str(e)}


class OpenVikingPostCallHook(Hook):
    name = "openviking_post_call"
    is_sync = True

    async def _get_client(self, workspace_id: str) -> VikingClient:
        # Use global singleton client
        return await get_global_client()

    async def _read_skill_memory(self, workspace_id: str, skill_name: str) -> str:
        ov_client = await self._get_client(workspace_id)
        config = load_config()
        openviking_config = config.ov_server
        # (f'openviking_config.mode={openviking_config.mode}')
        if not skill_name:
            return ""
        try:
            if openviking_config.mode == "local":
                skill_memory_uri = f"viking://agent/ffb1327b18bf/memories/skills/{skill_name}.md"
            else:
                agent_space_name = resolve_agent_memory_identity(config).agent_space_name
                skill_memory_uri = (
                    f"viking://agent/{agent_space_name}/memories/skills/{skill_name}.md"
                )
            content = await ov_client.read_content(skill_memory_uri, level="read")
            # print(f'content={content}')
            # logger.warning(f"content={content}")
            return f"\n\n---\n## Skill Memory\n{content}" if content else ""
        except Exception as e:
            logger.warning(f"Failed to read skill memory for {skill_name}: {e}")
            return ""

    async def execute(self, context: HookContext, tool_name, params, result) -> Any:
        if tool_name == "read_file":
            if result and not isinstance(result, Exception):
                match = re.search(r"^---\s*\nname:\s*(.+?)\s*\n", result, re.MULTILINE)
                if match:
                    skill_name = match.group(1).strip()
                    # logger.debug(f"skill_name={skill_name}")

                    agent_space_name = context.workspace_id
                    # logger.debug(f"agent_space_name={agent_space_name}")

                    skill_memory = await self._read_skill_memory(agent_space_name, skill_name)
                    # logger.debug(f"skill_memory={skill_memory}")
                    if skill_memory:
                        result = f"{result}{skill_memory}"

        return {"tool_name": tool_name, "params": params, "result": result}


hooks = {"message.compact": [OpenVikingCompactHook()], "tool.post_call": [OpenVikingPostCallHook()]}
