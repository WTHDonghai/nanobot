# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

import json

import vikingbot.config.loader as config_loader
from vikingbot.config.schema import DEFAULT_HUMAN_HANDOFF_ENTRY_URL
from vikingbot.config.loader import (
    _apply_runtime_ov_server_overrides,
    _merge_ov_server_config,
    load_config,
)


def test_load_config_reads_human_handoff_entry_url_from_ov_conf(
    tmp_path, monkeypatch
) -> None:
    config_path = tmp_path / "ov.conf"
    entry_url = "https://cschat.antcloud.com.cn/index.htm?tntInstId=yLS_FlpK&scene=SCE01205703"
    config_path.write_text(
        json.dumps(
            {
                "bot": {
                    "tools": {
                        "human_handoff": {
                            "entry_url": entry_url,
                        }
                    }
                }
            }
        )
    )

    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    config = load_config()

    assert config.tools.human_handoff.entry_url == entry_url


def test_default_human_handoff_entry_url_is_obvious_placeholder() -> None:
    assert DEFAULT_HUMAN_HANDOFF_ENTRY_URL == (
        "https://human-handoff-url-not-configured.invalid/"
    )


def test_merge_ov_server_config_uses_loopback_for_wildcard_bind_host() -> None:
    bot_data = {}
    ov_data = {"host": "0.0.0.0", "port": 1933, "root_api_key": "test-root"}

    _merge_ov_server_config(bot_data, ov_data)

    assert bot_data["server_url"] == "http://127.0.0.1:1933"
    assert bot_data["root_api_key"] == "test-root"
    assert bot_data["mode"] == "remote"


def test_merge_ov_server_config_preserves_explicit_server_url() -> None:
    bot_data = {"server_url": "http://openviking:1933"}
    ov_data = {"host": "0.0.0.0", "port": 1933, "root_api_key": "test-root"}

    _merge_ov_server_config(bot_data, ov_data)

    assert bot_data["server_url"] == "http://openviking:1933"


def test_runtime_ov_server_override_wins_over_config(monkeypatch) -> None:
    bot_data = {"server_url": "http://127.0.0.1:1933"}

    monkeypatch.setenv("VIKINGBOT_OV_SERVER_URL", "http://127.0.0.1:1934")
    _apply_runtime_ov_server_overrides(bot_data)

    assert bot_data["server_url"] == "http://127.0.0.1:1934"
