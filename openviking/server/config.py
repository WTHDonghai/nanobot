# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""Server configuration for OpenViking HTTP Server."""

import sys
from typing import List, Optional

from pydantic import BaseModel, Field, ValidationError

from openviking_cli.utils import get_logger
from openviking_cli.utils.config.config_loader import (
    load_json_config,
    resolve_config_path,
)
from openviking_cli.utils.config.config_utils import format_validation_error
from openviking_cli.utils.config.consts import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_OV_CONF,
    OPENVIKING_CONFIG_ENV,
    SYSTEM_CONFIG_DIR,
)

logger = get_logger(__name__)


class PrometheusConfig(BaseModel):
    """Prometheus exporter configuration."""

    enabled: bool = False

    model_config = {"extra": "forbid"}


class TelemetryConfig(BaseModel):
    """Telemetry configuration."""

    prometheus: PrometheusConfig = Field(default_factory=PrometheusConfig)

    model_config = {"extra": "forbid"}


class PublicBotConfig(BaseModel):
    """Anonymous visitor access for public bot pages."""

    enabled: bool = False
    account_id: str = "default"
    agent_id: str = "default"
    cookie_name: str = "ov_guest_id"
    cookie_max_age_seconds: int = 60 * 60 * 24 * 180
    secret: Optional[str] = None
    user_id_prefix: str = "guest"

    model_config = {"extra": "forbid"}


class ServerConfig(BaseModel):
    """Server configuration (from the ``server`` section of ov.conf)."""

    host: str = "127.0.0.1"
    port: int = 1933
    workers: int = 1
    auth_mode: str = "api_key"
    root_api_key: Optional[str] = None
    cors_origins: List[str] = Field(default_factory=lambda: ["*"])
    with_bot: bool = False  # Enable Bot API proxy to Vikingbot
    bot_api_url: str = "http://localhost:18790"  # Vikingbot OpenAPIChannel URL (default port)
    encryption_enabled: bool = False  # Whether API key hashing is enabled
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    public_bot: PublicBotConfig = Field(default_factory=PublicBotConfig)

    model_config = {"extra": "forbid"}


def load_server_config(config_path: Optional[str] = None) -> ServerConfig:
    """Load server configuration from ov.conf.

    Reads the ``server`` section of ov.conf and also ensures the full
    ov.conf is loaded into the OpenVikingConfigSingleton so that model
    and storage settings are available.

    Resolution chain:
      1. Explicit ``config_path`` (from --config)
      2. OPENVIKING_CONFIG_FILE environment variable
      3. ~/.openviking/ov.conf

    Args:
        config_path: Explicit path to ov.conf.

    Returns:
        ServerConfig instance with defaults for missing fields.

    Raises:
        FileNotFoundError: If no config file is found.
    """
    path = resolve_config_path(config_path, OPENVIKING_CONFIG_ENV, DEFAULT_OV_CONF)
    if path is None:
        default_path_user = DEFAULT_CONFIG_DIR / DEFAULT_OV_CONF
        default_path_system = SYSTEM_CONFIG_DIR / DEFAULT_OV_CONF
        raise FileNotFoundError(
            f"OpenViking configuration file not found.\n"
            f"Please create {default_path_user} or {default_path_system}, or set {OPENVIKING_CONFIG_ENV}.\n"
            f"See: https://openviking.dev/docs/guides/configuration"
        )

    data = load_json_config(path)
    server_data = data.get("server", {})
    if server_data is None:
        server_data = {}
    if not isinstance(server_data, dict):
        raise ValueError("Invalid server config: 'server' section must be an object")

    # Get encryption enabled from config data directly (for test compatibility)
    encryption_enabled = data.get("encryption", {}).get("enabled", False)

    bot_data = data.get("bot", {})
    if not isinstance(bot_data, dict):
        bot_data = {}
    bot_ov_server = bot_data.get("ov_server", {})
    if not isinstance(bot_ov_server, dict):
        bot_ov_server = {}

    public_bot_data = server_data.get("public_bot")
    if isinstance(public_bot_data, dict):
        public_bot_data = dict(public_bot_data)
        public_bot_data.setdefault("account_id", bot_ov_server.get("account_id", "default"))
        public_bot_data.setdefault("agent_id", bot_ov_server.get("agent_id", "default"))
        server_data = dict(server_data)
        server_data["public_bot"] = public_bot_data

    try:
        config = ServerConfig.model_validate(server_data)
    except ValidationError as e:
        raise ValueError(
            f"Invalid server config in {path}:\n"
            f"{format_validation_error(root_model=ServerConfig, error=e, path_prefix='server')}"
        ) from e

    return config.model_copy(update={"encryption_enabled": encryption_enabled})


_LOCALHOST_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _is_localhost(host: str) -> bool:
    """Return True if *host* resolves to a loopback address."""
    return host in _LOCALHOST_HOSTS


def validate_server_config(config: ServerConfig) -> None:
    """Validate server config for safe startup.

    In ``api_key`` mode, when ``root_api_key`` is not set, authentication is
    disabled (dev mode). This is only acceptable when the server binds to
    localhost. Binding to a non-loopback address without authentication
    exposes an unauthenticated ROOT endpoint to the network.

    Raises:
        SystemExit: If the configuration is unsafe.
    """
    if config.auth_mode not in {"api_key", "trusted"}:
        logger.error(
            "Invalid server.auth_mode=%r. Expected one of: 'api_key', 'trusted'.",
            config.auth_mode,
        )
        sys.exit(1)

    if config.auth_mode == "trusted":
        if config.public_bot.enabled:
            logger.error(
                "server.public_bot.enabled requires server.auth_mode='api_key'. "
                "Trusted mode is intended for identity-injecting gateways."
            )
            sys.exit(1)
        if not _is_localhost(config.host):
            logger.warning(
                "SECURITY: server.auth_mode='trusted' on non-localhost host '%s'. "
                "Only use this behind a trusted network boundary or identity-injecting gateway.",
                config.host,
            )
        return

    if config.public_bot.enabled and not config.root_api_key:
        logger.error(
            "server.public_bot.enabled requires server.root_api_key so anonymous visitors can be "
            "mapped into managed users safely."
        )
        sys.exit(1)

    if config.root_api_key:
        return

    if not _is_localhost(config.host):
        logger.error(
            "SECURITY: server.root_api_key is not configured and server.host "
            "is '%s' (non-localhost). This would expose an unauthenticated "
            "ROOT endpoint to the network.",
            config.host,
        )
        logger.error(
            "To fix, either:\n"
            "  1. Set server.root_api_key in ov.conf, or\n"
            '  2. Bind to localhost (server.host = "127.0.0.1")'
        )
        sys.exit(1)
