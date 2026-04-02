"""Human handoff service shared by HTTP APIs and agent tools."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from vikingbot.config.schema import HumanHandoffToolConfig


@dataclass(slots=True)
class HumanHandoffPayload:
    """Normalized payload for requesting a human handoff."""

    session_id: str | None = None
    user_id: str | None = None
    reason: str | None = None
    summary: str | None = None
    latest_user_message: str | None = None
    latest_assistant_message: str | None = None
    source: str = "api"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-safe dict without empty values."""
        return {
            key: value
            for key, value in asdict(self).items()
            if value not in (None, "", {}, [])
        }


@dataclass(slots=True)
class HumanHandoffResult:
    """Normalized result returned by the handoff service."""

    success: bool
    status: str
    message: str
    handoff_id: str | None = None
    entry_url: str | None = None
    service_response: dict[str, Any] = field(default_factory=dict)


class HumanHandoffService:
    """Create or resolve a handoff entry for a human support flow."""

    def __init__(self, config: HumanHandoffToolConfig | None = None):
        self.config = config or HumanHandoffToolConfig()

    async def request_handoff(self, payload: HumanHandoffPayload) -> HumanHandoffResult:
        """Request a human handoff or fall back to a configured entry URL."""
        if not self.config.enabled:
            raise RuntimeError("Human handoff service is disabled.")

        if self.config.service_url:
            return await self._request_remote_handoff(payload)

        if self.config.entry_url:
            return HumanHandoffResult(
                success=True,
                status="ready",
                message="已为您准备转人工服务入口。",
                entry_url=self.config.entry_url,
                service_response={"mode": "entry_url_only"},
            )

        raise RuntimeError("Human handoff service is not configured.")

    async def _request_remote_handoff(self, payload: HumanHandoffPayload) -> HumanHandoffResult:
        response_data: dict[str, Any]
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(
                self.config.service_url,
                json=payload.to_dict(),
                headers=self.config.extra_headers,
            )
            response.raise_for_status()
            response_data = self._parse_response(response)

        success = bool(response_data.get("success", True))
        status = str(response_data.get("status") or response_data.get("result") or "accepted")
        message = str(
            response_data.get("message")
            or response_data.get("detail")
            or ("转人工请求已提交。" if success else "转人工请求提交失败。")
        )
        handoff_id = self._pick_first_non_empty(
            response_data.get("handoff_id"),
            response_data.get("ticket_id"),
            response_data.get("id"),
        )
        entry_url = self._pick_first_non_empty(
            response_data.get("entry_url"),
            response_data.get("redirect_url"),
            response_data.get("url"),
            self.config.entry_url,
        )

        return HumanHandoffResult(
            success=success,
            status=status,
            message=message,
            handoff_id=handoff_id,
            entry_url=entry_url,
            service_response=response_data,
        )

    def _parse_response(self, response: httpx.Response) -> dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type:
            data = response.json()
            return data if isinstance(data, dict) else {"data": data}
        return {"text": response.text}

    @staticmethod
    def _pick_first_non_empty(*values: Any) -> str | None:
        for value in values:
            if value in (None, ""):
                continue
            return str(value)
        return None
