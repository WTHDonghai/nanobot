# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: Apache-2.0

from vikingbot.config.loader import _merge_ov_server_config


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
