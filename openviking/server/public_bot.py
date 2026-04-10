"""Anonymous visitor identity resolution for public bot access."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
from typing import Optional

from fastapi import Request

from openviking.server.api_keys import APIKeyManager
from openviking.server.config import PublicBotConfig
from openviking.server.identity import RequestContext, ResolvedIdentity, Role
from openviking.service.core import OpenVikingService
from openviking_cli.exceptions import AlreadyExistsError, InvalidArgumentError, NotFoundError
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils import get_logger

logger = get_logger(__name__)

_ALLOWED_EXACT_PATHS = {
    "/api/v1/system/whoami",
    "/api/v1/sessions",
    "/bot/v1/chat",
    "/bot/v1/chat/stream",
    "/bot/v1/handoff",
}
_ALLOWED_PREFIXES = (
    "/api/v1/sessions/",
)
_VISITOR_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_SAFE_ID_CHARS_RE = re.compile(r"[^a-zA-Z0-9_-]+")


class PublicBotIdentityResolver:
    """Map anonymous browser visitors to stable OpenViking users."""

    def __init__(
        self,
        config: PublicBotConfig,
        api_key_manager: APIKeyManager,
        service: OpenVikingService,
        root_api_key: str,
    ):
        self.config = config
        self._api_key_manager = api_key_manager
        self._service = service
        secret = (config.secret or root_api_key or "").strip()
        if not secret:
            raise ValueError("public bot identity resolver requires a non-empty secret")
        self._secret = secret.encode("utf-8")
        self._known_users: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def validate(self) -> None:
        """Verify that the configured public account already exists."""
        account_ids = {entry["account_id"] for entry in self._api_key_manager.get_accounts()}
        if self.config.account_id not in account_ids:
            raise InvalidArgumentError(
                f"server.public_bot.account_id '{self.config.account_id}' does not exist. "
                "Create the account before enabling anonymous bot access."
            )

    def is_enabled_for_path(self, path: str) -> bool:
        return self.config.enabled and (
            path in _ALLOWED_EXACT_PATHS or any(path.startswith(prefix) for prefix in _ALLOWED_PREFIXES)
        )

    async def resolve(
        self,
        request: Request,
        requested_agent_id: Optional[str] = None,
    ) -> Optional[ResolvedIdentity]:
        """Resolve an anonymous visitor into a stable tenant-scoped user."""
        path = request.url.path
        if not self.is_enabled_for_path(path):
            return None

        visitor_id = self._read_signed_visitor_id(request)
        if visitor_id is None:
            visitor_id = secrets.token_hex(16)
            request.state.public_bot_cookie_value = self._sign_visitor_id(visitor_id)

        account_id = self.config.account_id or "default"
        agent_id = requested_agent_id or self.config.agent_id or "default"
        user_id = self._derive_user_id(account_id, visitor_id)
        request.state.public_bot_user_id = user_id

        await self._ensure_user_initialized(account_id, user_id, agent_id)

        return ResolvedIdentity(
            role=Role.USER,
            account_id=account_id,
            user_id=user_id,
            agent_id=agent_id,
        )

    def apply_cookie(self, request: Request, response) -> None:
        """Attach the anonymous visitor cookie to the outgoing response."""
        cookie_value = getattr(request.state, "public_bot_cookie_value", None)
        if not cookie_value:
            return
        response.set_cookie(
            key=self.config.cookie_name,
            value=cookie_value,
            max_age=self.config.cookie_max_age_seconds,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )

    def _read_signed_visitor_id(self, request: Request) -> Optional[str]:
        cookie_value = request.cookies.get(self.config.cookie_name)
        if not cookie_value:
            return None

        try:
            visitor_id, signature = cookie_value.split(".", 1)
        except ValueError:
            return None

        if not _VISITOR_ID_RE.fullmatch(visitor_id):
            return None

        expected_signature = self._sign_payload(visitor_id)
        if not hmac.compare_digest(signature, expected_signature):
            logger.warning("Discarded invalid public bot visitor cookie for path %s", request.url.path)
            return None
        return visitor_id

    def _sign_visitor_id(self, visitor_id: str) -> str:
        return f"{visitor_id}.{self._sign_payload(visitor_id)}"

    def _sign_payload(self, payload: str) -> str:
        return hmac.new(self._secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def _derive_user_id(self, account_id: str, visitor_id: str) -> str:
        prefix = _SAFE_ID_CHARS_RE.sub("_", self.config.user_id_prefix or "guest").strip("_") or "guest"
        digest = hashlib.sha256(
            f"{account_id}:{visitor_id}:{self._secret.decode('utf-8')}".encode("utf-8")
        ).hexdigest()[:24]
        return f"{prefix}_{digest}"

    async def _ensure_user_initialized(self, account_id: str, user_id: str, agent_id: str) -> None:
        if user_id in self._known_users:
            return

        lock = await self._get_lock(user_id)
        async with lock:
            if user_id in self._known_users:
                return

            try:
                await self._api_key_manager.register_user(account_id, user_id, role="user")
            except AlreadyExistsError:
                pass
            except NotFoundError as exc:
                raise InvalidArgumentError(
                    f"server.public_bot.account_id '{account_id}' does not exist. "
                    "Create the account before serving anonymous bot users."
                ) from exc

            ctx = RequestContext(
                user=UserIdentifier(account_id, user_id, agent_id),
                role=Role.USER,
            )
            await self._service.initialize_user_directories(ctx)
            await self._service.initialize_agent_directories(ctx)
            self._known_users.add(user_id)

    async def _get_lock(self, user_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            lock = self._locks.get(user_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[user_id] = lock
            return lock

