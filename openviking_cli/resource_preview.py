"""Signed capability tokens for bot resource previews."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

RESOURCE_PREVIEW_SECRET_ENV = "OPENVIKING_BOT_RESOURCE_PREVIEW_TOKEN"
RESOURCE_PREVIEW_TTL_SECONDS = 24 * 60 * 60


class ResourcePreviewTokenError(ValueError):
    """Raised when a resource preview capability is invalid or expired."""


@dataclass(frozen=True)
class ResourcePreviewClaims:
    uri: str
    account_id: str
    expires_at: int


def _encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def create_resource_preview_token(
    *,
    uri: str,
    account_id: str,
    secret: str,
    ttl_seconds: int = RESOURCE_PREVIEW_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """Create a signed, tenant-bound resource preview capability."""
    normalized_uri = str(uri or "").strip().rstrip("/")
    normalized_account = str(account_id or "").strip()
    normalized_secret = str(secret or "").strip()
    if not normalized_uri.startswith("viking://resources/"):
        raise ResourcePreviewTokenError("Invalid resource URI")
    if not normalized_account:
        raise ResourcePreviewTokenError("Missing account ID")
    if not normalized_secret:
        raise ResourcePreviewTokenError("Missing preview secret")
    if ttl_seconds <= 0:
        raise ResourcePreviewTokenError("Preview TTL must be positive")

    payload = {
        "account_id": normalized_account,
        "exp": int(now if now is not None else time.time()) + int(ttl_seconds),
        "uri": normalized_uri,
    }
    encoded_payload = _encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )
    signature = hmac.new(
        normalized_secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded_payload}.{_encode(signature)}"


def verify_resource_preview_token(
    token: str,
    *,
    secret: str,
    expected_uri: str | None = None,
    now: int | None = None,
) -> ResourcePreviewClaims:
    """Verify a capability token and return its normalized claims."""
    normalized_secret = str(secret or "").strip()
    if not normalized_secret:
        raise ResourcePreviewTokenError("Missing preview secret")
    try:
        encoded_payload, encoded_signature = str(token or "").split(".", 1)
        supplied_signature = _decode(encoded_signature)
    except (ValueError, TypeError) as exc:
        raise ResourcePreviewTokenError("Malformed preview token") from exc

    expected_signature = hmac.new(
        normalized_secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise ResourcePreviewTokenError("Invalid preview token signature")

    try:
        payload = json.loads(_decode(encoded_payload).decode("utf-8"))
        uri = str(payload["uri"]).strip().rstrip("/")
        account_id = str(payload["account_id"]).strip()
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResourcePreviewTokenError("Invalid preview token payload") from exc

    if not uri.startswith("viking://resources/") or not account_id:
        raise ResourcePreviewTokenError("Invalid preview token claims")
    if expires_at <= int(now if now is not None else time.time()):
        raise ResourcePreviewTokenError("Preview token expired")
    if expected_uri is not None and uri != str(expected_uri).strip().rstrip("/"):
        raise ResourcePreviewTokenError("Preview token URI mismatch")

    return ResourcePreviewClaims(uri=uri, account_id=account_id, expires_at=expires_at)
