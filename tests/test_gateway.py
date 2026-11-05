"""Mocked model gateway tests: no external provider calls or real credentials."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from typing import Any

import httpx
import pytest

from max_core.gateway import (
    ChatMessage,
    Dialect,
    Gateway,
    GatewayError,
    GatewayRequest,
    HTTPAdapter,
    InferenceDestination,
    ModelSpec,
    Privacy,
)

OPENAI_DATA = {
    "id": "abc",
    "model": "demo-chat",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 40, "completion_tokens": 9, "total_tokens": 49},
}
ANTHROPIC_DATA = {
    "id": "msg_mock",
    "type": "message",
    "role": "assistant",
    "model": "demo-claude",
    "content": [{"type": "text", "text": "Answer "}, {"type": "text", "text": "ready"}],
    "usage": {"input_tokens": 34, "output_tokens": 12},
    "stop_reason": "end_turn",
}


def local(**overrides: Any) -> ModelSpec:
    return replace(
        ModelSpec(
            provider_id="local",
            model_id="demo-chat",
            dialect=Dialect.OPENAI_CHAT,
            endpoint="http://127.0.0.1:14000/v1/chat/completions",
            is_local=True,
            context_tokens=16_000,
            inference_destination=InferenceDestination.VERIFIED_LOCAL,
            input_usd_per_million=Decimal(0),
            output_usd_per_million=Decimal(0),
        ),
        **overrides,
    )


def cloud(**overrides: Any) -> ModelSpec:
    return replace(
        ModelSpec(
            provider_id="cloud",
            model_id="demo-chat",
            dialect=Dialect.OPENAI_CHAT,
            endpoint="https://api.example.test/v1/chat/completions",
            is_local=False,
            context_tokens=16_000,
            inference_destination=InferenceDestination.CLOUD,
            input_usd_per_million=Decimal("1"),
            output_usd_per_million=Decimal("3"),
            api_key="dummy-credential-not-real",
        ),
        **overrides,
    )


def anthropic(**overrides: Any) -> ModelSpec:
    return replace(
        ModelSpec(
            provider_id="claude-proxy",
            model_id="demo-claude",
            dialect=Dialect.ANTHROPIC_MESSAGES,
            endpoint="https://api.example.test/v1/messages",
            is_local=False,
            context_tokens=16_000,
            inference_destination=InferenceDestination.CLOUD,
            input_usd_per_million=Decimal("1"),
            output_usd_per_million=Decimal("1"),
            api_key="placeholder-not-secret",
        ),
        **overrides,
    )


def request(**overrides: Any) -> GatewayRequest:
    return replace(
        GatewayRequest(
            request_id="fixture-001",
            messages=(ChatMessage("system", "Be accurate"), ChatMessage("user", "Hi")),
            privacy=Privacy.PUBLIC,
            max_input_tokens=200,
            max_output_tokens=100,
        ),
        **overrides,
    )


def run_complete(models: list[ModelSpec], handler: Any, req: GatewayRequest | None = None) -> Any:
    async def call() -> Any:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await Gateway(models, HTTPAdapter(client)).complete(req or request())

    return asyncio.run(call())


def respond_json(data: dict[str, Any], *, status: int = 200) -> Any:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=data)

    return handler


def test_openai_normalization_and_request_schema() -> None:
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        assert "authorization" not in req.headers
        return httpx.Response(200, json=OPENAI_DATA)

    result = run_complete([local()], handler)
    assert result.content == "Hello"
    assert result.finish_reason == "stop"
    assert result.usage and result.usage.input_tokens == 40
    assert result.usage.output_tokens == 9
    assert result.estimated_cost_usd == Decimal(0)
    assert result.tool_proposals == ()
    assert seen[0]["messages"][0] == {"role": "system", "content": "Be accurate"}
    assert seen[0]["max_tokens"] == 100 and seen[0]["stream"] is False


def test_anthropic_system_messages_and_headers() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["x-api-key"] == "placeholder-not-secret"
        assert req.headers["anthropic-version"] == "2023-06-01"
        assert "authorization" not in req.headers
        payload = json.loads(req.content)
        assert payload["system"] == "Be accurate"
        assert payload["messages"] == [{"role": "user", "content": "Hi"}]
        return httpx.Response(200, json=ANTHROPIC_DATA)

    r = run_complete([anthropic()], handler, request(cloud_allowed=True, max_cost_usd=Decimal("1")))
    assert r.content == "Answer ready"
    assert r.usage and r.usage.input_tokens == 34
    assert r.estimated_cost_usd == Decimal("0.000046")


def test_openai_tool_calls_are_inert_proposals() -> None:
    data = {
        **OPENAI_DATA,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call1",
                            "function": {
                                "name": "read_note",
                                "arguments": '{"id":"n1"}',
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    result = run_complete([local()], respond_json(data))
    assert result.tool_proposals[0].name == "read_note"
    assert result.tool_proposals[0].arguments == {"id": "n1"}
    assert not hasattr(result.tool_proposals[0], "execute")


def test_anthropic_tool_use_is_inert() -> None:
    data = {
        **ANTHROPIC_DATA,
        "content": [
            {"type": "tool_use", "id": "tool_1", "name": "search_notes", "input": {"query": "demo"}}
        ],
    }
    r = run_complete(
        [anthropic()], respond_json(data), request(cloud_allowed=True, max_cost_usd=Decimal("1"))
    )
    assert r.tool_proposals[0].arguments["query"] == "demo"


@pytest.mark.parametrize("bad", ["{not-json", "[]", "null", '"quoted"'])
def test_invalid_tool_argument_payloads_rejected(bad: str) -> None:
    data = {
        **OPENAI_DATA,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "read_note", "arguments": bad}}
                    ],
                }
            }
        ],
    }
    with pytest.raises(GatewayError, match="invalid_tool_proposal"):
        run_complete([local()], respond_json(data))


def test_missing_usage_remains_unknown() -> None:
    data = {**OPENAI_DATA}
    del data["usage"]
    result = run_complete([local()], respond_json(data))
    assert result.usage is None and result.estimated_cost_usd is None


def test_remote_needs_explicit_cloud_permission_and_budget() -> None:
    for bad in [
        request(),
        request(cloud_allowed=True),
        request(cloud_allowed=True, max_cost_usd=Decimal("0.0001")),
    ]:
        with pytest.raises(GatewayError, match="no_eligible_provider"):
            run_complete([cloud()], lambda _: pytest.fail("Remote network was attempted"), bad)
    good = request(cloud_allowed=True, max_cost_usd=Decimal("0.01"))
    assert run_complete([cloud()], respond_json(OPENAI_DATA), good).provider_id == "cloud"


@pytest.mark.parametrize("privacy", [Privacy.CONFIDENTIAL, Privacy.RESTRICTED])
def test_sensitive_content_cannot_route_to_cloud(privacy: Privacy) -> None:
    req = request(privacy=privacy, cloud_allowed=True, max_cost_usd=Decimal("100"))
    with pytest.raises(GatewayError, match="no_eligible_provider"):
        run_complete([cloud()], lambda _: pytest.fail("Cloud must not be contacted"), req)


def test_local_only_filters_cloud_regardless_of_priority() -> None:
    r = run_complete(
        [cloud(priority=0), local(priority=50)],
        respond_json(OPENAI_DATA),
        request(cloud_allowed=True, local_only=True, max_cost_usd=Decimal("1")),
    )
    assert r.provider_id == "local"


def test_context_capability_model_and_region_gates() -> None:
    gateway = Gateway(
        [local(), cloud(capabilities=frozenset({"text", "tools"}), region="us")],
        HTTPAdapter(httpx.AsyncClient()),
    )
    assert not gateway.eligible(request(max_input_tokens=15_999, max_output_tokens=100))
    assert not gateway.eligible(request(required_capabilities=frozenset({"tools"})))
    assert not gateway.eligible(request(allowed_models=frozenset({"nonexistent"})))
    assert not gateway.eligible(request(allowed_regions=frozenset({"eu"})))
    allowed = request(
        cloud_allowed=True,
        max_cost_usd=Decimal("1"),
        required_capabilities=frozenset({"text", "tools"}),
        allowed_regions=frozenset({"us"}),
    )
    assert [s.provider_id for s in gateway.eligible(allowed)] == ["cloud"]


def test_unknown_remote_pricing_is_blocked() -> None:
    with pytest.raises(GatewayError, match="no_eligible_provider"):
        run_complete(
            [cloud(input_usd_per_million=None)],
            lambda _: pytest.fail("Never route unknown remote price"),
            request(cloud_allowed=True, max_cost_usd=Decimal("100")),
        )


def test_postflight_over_budget_is_reported() -> None:
    data = {**OPENAI_DATA, "usage": {"prompt_tokens": 2_000_000, "completion_tokens": 100}}
    with pytest.raises(GatewayError, match="budget_exceeded"):
        run_complete(
            [cloud()],
            respond_json(data),
            request(cloud_allowed=True, max_cost_usd=Decimal("0.001")),
        )


def test_auth_error_is_sanitized() -> None:
    value = "private-upstream-error-should-not-be-disclosed"
    with pytest.raises(GatewayError) as exc:
        run_complete(
            [cloud()],
            lambda _: httpx.Response(401, text=value),
            request(cloud_allowed=True, max_cost_usd=Decimal("1")),
        )
    assert exc.value.code == "auth_failure"
    assert value not in str(exc.value)
    assert "dummy-credential" not in str(exc.value)
    assert "dummy-credential" not in repr(cloud())


def test_redirect_not_followed_with_credentials() -> None:
    calls: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(str(r.url))
        return httpx.Response(307, headers={"location": "https://attacker.invalid/steal"})

    with pytest.raises(GatewayError, match="provider_error"):
        run_complete([cloud()], handler, request(cloud_allowed=True, max_cost_usd=Decimal("1")))
    assert len(calls) == 1


def test_fallback_on_429_only_when_explicit() -> None:
    calls: list[str] = []

    def handler(r: httpx.Request) -> httpx.Response:
        calls.append(r.url.host or "")
        if r.url.host == "first.example.test":
            return httpx.Response(429)
        return httpx.Response(200, json=OPENAI_DATA)

    first = cloud(
        provider_id="first", priority=0, endpoint="https://first.example.test/v1/chat/completions"
    )
    second = cloud(
        provider_id="second", priority=1, endpoint="https://second.example.test/v1/chat/completions"
    )
    req = request(cloud_allowed=True, max_cost_usd=Decimal("1"), allow_fallback=True)
    assert run_complete([first, second], handler, req).provider_id == "second"
    assert calls == ["first.example.test", "second.example.test"]
    calls.clear()
    with pytest.raises(GatewayError, match="rate_limited"):
        run_complete([first, second], handler, replace(req, allow_fallback=False))
    assert len(calls) == 1


def test_invalid_body_never_falls_back() -> None:
    hits = 0

    def handler(_r: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(200, json={"broken": True})

    with pytest.raises(GatewayError, match="malformed_provider_response"):
        run_complete(
            [local(provider_id="a"), local(provider_id="b")], handler, request(allow_fallback=True)
        )
    assert hits == 1


def test_model_mismatch_is_rejected() -> None:
    with pytest.raises(GatewayError, match="model_mismatch"):
        run_complete([local()], respond_json({**OPENAI_DATA, "model": "different"}))


def test_duplicate_model_config_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        Gateway([local(), local()], HTTPAdapter(httpx.AsyncClient()))


@pytest.mark.parametrize(
    "endpoint,is_local",
    [
        ("http://api.example.test/chat", False),
        ("http://169.254.169.254/latest", True),
        ("http://127.0.0.1:4040/", True),
        ("***api.example.test/chat", False),
        ("https://api.example.test/chat?key=test", False),
    ],
)
def test_invalid_or_unsafe_endpoints(endpoint: str, is_local: bool) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        local(endpoint=endpoint, is_local=is_local)


def test_large_body_rejected() -> None:
    with pytest.raises(GatewayError, match="response_too_large"):
        run_complete([local()], lambda _: httpx.Response(200, text="x" * 1_000_200))


def test_no_automatic_provider_registration() -> None:
    with pytest.raises(GatewayError, match="no_eligible_provider"):
        run_complete([], lambda _: pytest.fail("No configured provider"))


def test_unrealistic_token_envelope_rejected() -> None:
    with pytest.raises(ValueError, match="conservative text envelope"):
        request(max_input_tokens=2)


def test_late_system_message_rejected() -> None:
    with pytest.raises(ValueError, match="System messages must precede"):
        request(messages=(ChatMessage("user", "Hi"), ChatMessage("system", "Late")))
