# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0
"""
Test that provider instruction correctly instructs LLM.
"""

import json

import pytest

from openviking.server.identity import RequestContext, Role
from openviking.session.memory.session_extract_context_provider import SessionExtractContextProvider
from openviking_cli.session.user_id import UserIdentifier
from openviking_cli.utils.config.open_viking_config import OpenVikingConfigSingleton


@pytest.fixture(autouse=True)
def _drain_background_tasks(monkeypatch, tmp_path):
    """Keep these provider-only tests independent from session client fixtures."""
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
    monkeypatch.setenv("OPENVIKING_CONFIG_FILE", str(config_path))
    OpenVikingConfigSingleton.reset_instance()
    yield
    OpenVikingConfigSingleton.reset_instance()


class TestProviderInstruction:
    """Test the provider instruction contains correct instructions."""

    def test_instruction_contains_read_before_edit_instructions(self):
        """Test that instruction explicitly tells LLM to read files before editing."""
        # Create provider with mock messages
        mock_messages = []
        provider = SessionExtractContextProvider(messages=mock_messages)

        instruction = provider.instruction()

        # Check for critical instructions
        assert (
            "Before editing ANY existing memory file, you MUST first read its complete content"
            in instruction
        )
        assert (
            "ONLY read URIs that are explicitly listed in ls tool results or returned by previous tool calls"
            in instruction
        )

    def test_instruction_contains_output_language(self):
        """Test that instruction includes the output language setting."""
        mock_messages = []
        provider = SessionExtractContextProvider(messages=mock_messages)

        instruction = provider.instruction()

        # Check that output language instruction is present
        assert "Target Output Language" in instruction
        assert "All memory content MUST be written in" in instruction

    def test_instruction_contains_feedback_signal_and_scope(self):
        """Test that feedback-driven extraction passes its signal into the prompt."""
        provider = SessionExtractContextProvider(
            messages=[],
            memory_scope="agent",
            feedback="The user marked this answer as helpful.",
        )

        instruction = provider.instruction()

        assert "Memory Scope" in instruction
        assert "`agent` scope" in instruction
        assert "User Feedback Signal" in instruction
        assert "The user marked this answer as helpful." in instruction

    def test_agent_scope_filters_user_memory_schemas(self):
        """Test that scoped extraction only exposes matching memory schemas."""
        provider = SessionExtractContextProvider(messages=[], memory_scope="agent")
        ctx = RequestContext(user=UserIdentifier.the_default_user(), role=Role.ROOT)

        schema_types = {schema.memory_type for schema in provider.get_memory_schemas(ctx)}

        assert schema_types
        assert schema_types <= {"cases", "patterns", "tools", "skills"}
        assert "profile" not in schema_types
        assert "preferences" not in schema_types
