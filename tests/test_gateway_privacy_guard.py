"""Synthetic-only cloud egress fixtures; no real credentials or network calls."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest

from max_core.gateway import ChatMessage, Gateway, GatewayError, HTTPAdapter
from max_core.gateway.privacy_guard import contains_high_confidence_secret
from tests.test_gateway import cloud, local, request, run_complete


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic",
        "ghp_" + "X" * 32,
        "github_pat_" + "A" * 32,
        "sk-proj-" + "z" * 32,
        "AKIA" + "A" * 16,
        "Authorization: Bearer " + "A" * 32,
        "export API_KEY='" + "z" * 24 + "'",
        "refresh_token: " + "x" * 28,
        "DATABASE_URL=postgresql://example.invalid/db",
        "GOCSPX-" + "P" * 24,
    ],
)
def test_detects_synthetic_credentials(text: str) -> None:
    assert contains_high_confidence_secret((ChatMessage("user", text),)) is True


def test_public_text_and_placeholders_do_not_trigger() -> None:
    messages = (ChatMessage("user", "Review [REDACTED_SECRET_1] and this traceback"),)
    assert contains_high_confidence_secret(messages) is False


def test_cloud_filtered_but_verified_local_remains_eligible() -> None:
    sensitive = replace(
        request(cloud_allowed=True, max_cost_usd=Decimal("1")),
        messages=(ChatMessage("user", "API_KEY=" + "s" * 28),),
    )
    gateway = Gateway([cloud(priority=0), local(priority=1)], HTTPAdapter(httpx.AsyncClient()))
    assert [spec.provider_id for spec in gateway.eligible(sensitive)] == ["local"]


def test_secret_prevents_cloud_fallback_and_http_request() -> None:
    sensitive = replace(
        request(cloud_allowed=True, allow_fallback=True, max_cost_usd=Decimal("1")),
        messages=(ChatMessage("user", "ghp_" + "Q" * 30),),
    )
    with pytest.raises(GatewayError, match="no_eligible_provider"):
        run_complete([cloud()], lambda _: pytest.fail("cloud request was sent"), sensitive)


@pytest.mark.parametrize("stream", [False, True])
def test_direct_http_adapter_also_blocks_cloud_secret(stream: bool) -> None:
    sensitive = replace(
        request(cloud_allowed=True, max_cost_usd=Decimal("1")),
        messages=(ChatMessage("user", "CLIENT_SECRET=" + "S" * 30),),
    )

    def handler(_req: httpx.Request) -> httpx.Response:
        pytest.fail("raw secret reached the transport")

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = HTTPAdapter(client)
            with pytest.raises(GatewayError, match="cloud_sensitive_content_blocked"):
                if stream:
                    async for _ in adapter.stream(cloud(), sensitive):
                        pass
                else:
                    await adapter.complete(cloud(), sensitive)

    asyncio.run(check())
