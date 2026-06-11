# Copyright (c) 2026 Beijing Volcano Engine Technology Co., Ltd.
# SPDX-License-Identifier: AGPL-3.0

"""Tests for signed resource preview capabilities."""

import pytest

from openviking_cli.resource_preview import (
    ResourcePreviewTokenError,
    create_resource_preview_token,
    verify_resource_preview_token,
)


def test_resource_preview_token_is_tenant_bound_and_expires() -> None:
    token = create_resource_preview_token(
        uri="viking://resources/docs/status.md",
        account_id="acme",
        secret="preview-secret",
        ttl_seconds=60,
        now=100,
    )

    claims = verify_resource_preview_token(
        token,
        secret="preview-secret",
        expected_uri="viking://resources/docs/status.md",
        now=159,
    )

    assert claims.account_id == "acme"
    assert claims.expires_at == 160
    with pytest.raises(ResourcePreviewTokenError, match="expired"):
        verify_resource_preview_token(token, secret="preview-secret", now=160)


def test_resource_preview_token_rejects_tampering_and_uri_reuse() -> None:
    token = create_resource_preview_token(
        uri="viking://resources/docs/status.md",
        account_id="acme",
        secret="preview-secret",
        now=100,
    )

    with pytest.raises(ResourcePreviewTokenError, match="signature"):
        verify_resource_preview_token(f"{token}x", secret="preview-secret", now=101)
    with pytest.raises(ResourcePreviewTokenError, match="URI mismatch"):
        verify_resource_preview_token(
            token,
            secret="preview-secret",
            expected_uri="viking://resources/docs/other.md",
            now=101,
        )
