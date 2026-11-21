"""Synthetic-only cloud egress fixtures; no real credentials or network calls."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from typing import Any

import httpx
import pytest

from max_core.gateway import ChatMessage, Gateway, GatewayError, HTTPAdapter, ToolProposal
from max_core.gateway.contracts import ToolDefinition
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


def test_tool_calls_arguments_tool_results_and_tool_defs_blocked_from_cloud() -> None:
    secret = "sk-proj-" + "z" * 32
    json_secret = '{"API_KEY":"' + "s" * 24 + '"}'
    gateway = Gateway(
        [
            cloud(capabilities=frozenset({"text", "tools"})),
            local(capabilities=frozenset({"text", "tools"})),
        ],
        HTTPAdapter(httpx.AsyncClient()),
    )

    with_tool_args = replace(
        request(cloud_allowed=True, max_cost_usd=Decimal("1"), max_input_tokens=500),
        messages=(
            ChatMessage("user", "Run tool"),
            ChatMessage(
                "assistant",
                "",
                tool_calls=(ToolProposal("c1", "fetch", {"token": secret}),),
            ),
            ChatMessage("tool", "ok", tool_call_id="c1"),
        ),
    )
    assert [s.provider_id for s in gateway.eligible(with_tool_args)] == ["local"]

    with_tool_json_output = replace(
        request(cloud_allowed=True, max_cost_usd=Decimal("1"), max_input_tokens=500),
        messages=(
            ChatMessage("user", "Run tool"),
            ChatMessage("assistant", "", tool_calls=(ToolProposal("c1", "fetch", {}),)),
            ChatMessage("tool", json_secret, tool_call_id="c1"),
        ),
    )
    assert [s.provider_id for s in gateway.eligible(with_tool_json_output)] == ["local"]

    with_tool_def = replace(
        request(cloud_allowed=True, max_cost_usd=Decimal("1"), max_input_tokens=500),
        tools=(ToolDefinition("fetch", f"Uses Bearer {'B' * 24}"),),
    )
    assert [s.provider_id for s in gateway.eligible(with_tool_def)] == ["local"]

    def fail_transport(_req: httpx.Request) -> httpx.Response:
        pytest.fail("secret in tool payload reached HTTP transport")

    async def check_adapter() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail_transport)) as client:
            adapter = HTTPAdapter(client)
            spec = cloud(capabilities=frozenset({"text", "tools"}))
            for blocked_req in (with_tool_args, with_tool_json_output, with_tool_def):
                with pytest.raises(GatewayError, match="cloud_sensitive_content_blocked"):
                    await adapter.complete(spec, blocked_req)
                with pytest.raises(GatewayError, match="cloud_sensitive_content_blocked"):
                    async for _ in adapter.stream(spec, blocked_req):
                        pass

    asyncio.run(check_adapter())


def test_json_in_string_prefixed_json_and_deep_nesting_blocked_from_cloud() -> None:
    from max_core.gateway.adapters import _walk_strings, request_has_high_confidence_secret

    gateway = Gateway(
        [
            cloud(capabilities=frozenset({"text", "tools"})),
            local(capabilities=frozenset({"text", "tools"})),
        ],
        HTTPAdapter(httpx.AsyncClient()),
    )

    nested_json_in_str = json.dumps(
        {
            "status": "completed",
            "output": json.dumps({"API_KEY": "mysecretvalue123456"}),
        },
        separators=(",", ":"),
    )
    prefixed_json = 'File contents: {"DATABASE_URL":"postgresql://user:pass@db.example/prod"}'
    noisy_prefixed = 'Log: {not json} {"CLIENT_SECRET":"supersecretclient123"}'

    deep_obj: dict[str, Any] = {"API_KEY": "deepsecretvalue123456"}
    for idx in range(40):
        deep_obj = {f"layer_{idx}": [deep_obj]}

    deep_tool_json = json.dumps(deep_obj, separators=(",", ":"))

    requests_to_block = [
        replace(
            request(cloud_allowed=True, max_cost_usd=Decimal("1"), max_input_tokens=1000),
            messages=(
                ChatMessage("user", "Read output"),
                ChatMessage("assistant", "", tool_calls=(ToolProposal("c1", "read", {}),)),
                ChatMessage("tool", nested_json_in_str, tool_call_id="c1"),
            ),
        ),
        replace(
            request(cloud_allowed=True, max_cost_usd=Decimal("1"), max_input_tokens=1000),
            messages=(
                ChatMessage("user", "Read output"),
                ChatMessage("assistant", "", tool_calls=(ToolProposal("c1", "read", {}),)),
                ChatMessage("tool", prefixed_json, tool_call_id="c1"),
            ),
        ),
        replace(
            request(cloud_allowed=True, max_cost_usd=Decimal("1"), max_input_tokens=1000),
            messages=(ChatMessage("user", noisy_prefixed),),
        ),
        replace(
            request(cloud_allowed=True, max_cost_usd=Decimal("1"), max_input_tokens=4000),
            messages=(
                ChatMessage("user", "Deep tool output"),
                ChatMessage("assistant", "", tool_calls=(ToolProposal("c1", "read", {}),)),
                ChatMessage("tool", deep_tool_json, tool_call_id="c1"),
            ),
        ),
        replace(
            request(cloud_allowed=True, max_cost_usd=Decimal("1"), max_input_tokens=4000),
            messages=(
                ChatMessage("user", "Deep tool args"),
                ChatMessage("assistant", "", tool_calls=(ToolProposal("c1", "read", deep_obj),)),
                ChatMessage("tool", "ok", tool_call_id="c1"),
            ),
        ),
        replace(
            request(cloud_allowed=True, max_cost_usd=Decimal("1"), max_input_tokens=4000),
            tools=(ToolDefinition("deep_tool", "Deep schema", deep_obj),),
        ),
    ]

    for blocked_req in requests_to_block:
        assert [s.provider_id for s in gateway.eligible(blocked_req)] == ["local"]
        assert request_has_high_confidence_secret(blocked_req.messages, blocked_req.tools) is True

    async def check_adapter() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("Secret leaked to transport"))
        ) as client:
            adapter = HTTPAdapter(client)
            spec = cloud(capabilities=frozenset({"text", "tools"}))
            for blocked_req in requests_to_block:
                with pytest.raises(GatewayError, match="cloud_sensitive_content_blocked"):
                    await adapter.complete(spec, blocked_req)
                with pytest.raises(GatewayError, match="cloud_sensitive_content_blocked"):
                    async for _ in adapter.stream(spec, blocked_req):
                        pass

    asyncio.run(check_adapter())

    # Cycle safety: self-referential dict/list terminates cleanly.
    cyclic: dict[str, Any] = {"ok": "hello"}
    cyclic["self"] = cyclic
    extracted: list[str] = []
    _walk_strings(cyclic, extracted)
    assert "ok=hello" in extracted


@pytest.mark.parametrize(
    "payload",
    [
        '{"a":' * 80 + " " * 1000 + '{"API_KEY":"mysecretvalue123456"}',
        '{"a":' * 400 + ' "x" ' + '{"API_KEY":"super_secret_token_12345"}',
        '{"a":' * 5 + " " * 25_000 + '{"API_KEY":"mysecretvalue123456"}',
        "[" * 500 + '{"API_KEY":"mysecretvalue123456"}',
        '{"API_KEY":"super_secret_token_12345"',
        '{"API_KEY":"super_secret_token_12345',
        '["API_KEY=super_secret_token_12345"',
        'Log: "{\\"API_KEY\\":\\"super_secret_token_12345\\"}"',
        'INFO line=1 payload="{\\"API_KEY\\":\\"mysecretvalue123456\\"}"',
        "File: " + json.dumps(json.dumps({"API_KEY": "mysecretvalue123456"})),
        "File: " + json.dumps(json.dumps(json.dumps({"API_KEY": "mysecretvalue123456"}))),
        'Log: {\\"API_KEY\\":\\"mysecretvalue123456\\"}',
        'Prefix "lone quote then {"API_KEY":"mysecretvalue123456"}',
        '{"note": "see {"API_KEY":"mysecretvalue123456"}',
        '{"a": "\\u0041PI_KEY=mysecretvalue123456"}',
        '{"msg": "Bearer\\t' + "A" * 24 + '"}',
        "Log: \\u0041PI_KEY=mysecretvalue123456",
        str({"API_KEY": "super_secret_token_12345"}),
        "{API_KEY: super_secret_token_12345}",
        '{"API_KEY": 12345678}',
        "[" * 4000 + '{"API_KEY":"super_secret_token_12345"}' + "]" * 4000,
    ],
)
def test_linear_m16_scanner_blocks_adversarial_embedded_secrets(payload: str) -> None:
    from max_core.gateway.adapters import request_has_high_confidence_secret

    user_req = replace(
        request(cloud_allowed=True, max_cost_usd=Decimal("1"), max_input_tokens=100_000),
        messages=(ChatMessage("user", payload),),
    )
    assert request_has_high_confidence_secret(user_req.messages, user_req.tools) is True

    async def check_adapter() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("Adversarial secret leaked"))
        ) as client:
            adapter = HTTPAdapter(client)
            spec = cloud()
            with pytest.raises(GatewayError, match="cloud_sensitive_content_blocked"):
                await adapter.complete(spec, user_req)
            with pytest.raises(GatewayError, match="cloud_sensitive_content_blocked"):
                async for _ in adapter.stream(spec, user_req):
                    pass

    asyncio.run(check_adapter())


def test_linear_m16_scanner_blocks_large_tool_payload_and_tool_description_secret() -> None:
    from max_core.gateway.adapters import request_has_high_confidence_secret

    large_prefix_payload = ('{"a":' * 10) + (" " * 15_000) + '{"API_KEY":"mysecretvalue123456"}'
    tool_msg_req = replace(
        request(cloud_allowed=True, max_cost_usd=Decimal("1"), max_input_tokens=50_000),
        messages=(
            ChatMessage("user", "Inspect output"),
            ChatMessage("assistant", "", tool_calls=(ToolProposal("c1", "read", {}),)),
            ChatMessage("tool", large_prefix_payload, tool_call_id="c1"),
        ),
    )
    assert request_has_high_confidence_secret(tool_msg_req.messages, tool_msg_req.tools) is True

    tool_desc_req = replace(
        request(cloud_allowed=True, max_cost_usd=Decimal("1"), max_input_tokens=2_000),
        tools=(ToolDefinition("fetch", 'Uses {"API_KEY":"super_secret_token_12345"}'),),
    )
    assert request_has_high_confidence_secret(tool_desc_req.messages, tool_desc_req.tools) is True


@pytest.mark.parametrize(
    "benign_payload",
    [
        "[" * 5000,
        '{"a":' * 5000,
        "[" * 3000 + "1" + "]" * 3000,
        '"' * 32_000,
        "\\" * 32_000,
        '{"API_KEY": "short"}',
        '{"API_KEY": true}',
        '{"API_KEY": 1234567}',
        '{"properties": {"api_key": {"type": "string", "description": "API key"}}}',
        '{"note": "Check API_KEY: it is documented in README"}',
    ],
)
def test_linear_m16_scanner_allows_benign_and_deep_brackets_without_recursion_error(
    benign_payload: str,
) -> None:
    from max_core.gateway.adapters import request_has_high_confidence_secret

    req = replace(
        request(cloud_allowed=True, max_cost_usd=Decimal("1"), max_input_tokens=100_000),
        messages=(ChatMessage("user", benign_payload),),
    )
    assert request_has_high_confidence_secret(req.messages, req.tools) is False


def test_request_has_secret_handles_depth_3000_containers_without_recursion_error() -> None:
    from max_core.gateway.adapters import request_has_high_confidence_secret

    deep_secret: dict[str, Any] = {"API_KEY": "deep_secret_token_12345"}
    deep_benign: dict[str, Any] = {"ok": "hello"}
    for _ in range(3000):
        deep_secret = {"a": deep_secret}
        deep_benign = {"a": deep_benign}

    call = ToolProposal("c1", "read", {})
    msg_secret = ChatMessage("assistant", "", tool_calls=(call,))
    object.__setattr__(call, "arguments", deep_secret)
    assert request_has_high_confidence_secret((msg_secret,)) is True

    call_benign = ToolProposal("c2", "read", {})
    msg_benign = ChatMessage("assistant", "", tool_calls=(call_benign,))
    object.__setattr__(call_benign, "arguments", deep_benign)
    assert request_has_high_confidence_secret((msg_benign,)) is False

    tool_def = ToolDefinition("read", "Read")
    object.__setattr__(tool_def, "parameters", deep_secret)
    assert request_has_high_confidence_secret((ChatMessage("user", "hi"),), (tool_def,)) is True
