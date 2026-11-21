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
    ToolProposal,
)
from max_core.gateway.contracts import ToolDefinition

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


def test_tool_definition_serialization_and_capability_gate() -> None:
    tool = ToolDefinition(
        name="read_note",
        description="Read a saved note by id.",
        parameters={"type": "object", "properties": {"id": {"type": "string"}}},
    )
    req = request(
        cloud_allowed=True,
        max_cost_usd=Decimal("1"),
        max_input_tokens=400,
        tools=(tool,),
    )
    # Without "tools" capability, Gateway.eligible denies the spec.
    with pytest.raises(GatewayError, match="no_eligible_provider"):
        run_complete([cloud()], lambda _: pytest.fail("Should not route without tools cap"), req)

    # Direct HTTPAdapter call also rejects missing tools capability.
    async def direct_denied() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("Should not send"))
        ) as client:
            with pytest.raises(GatewayError, match="unsupported_capability"):
                await HTTPAdapter(client).complete(cloud(), req)

    asyncio.run(direct_denied())

    # OpenAI tool serialization
    seen_openai: list[dict[str, Any]] = []

    def openai_handler(r: httpx.Request) -> httpx.Response:
        seen_openai.append(json.loads(r.content))
        return httpx.Response(200, json=OPENAI_DATA)

    run_complete([cloud(capabilities=frozenset({"text", "tools"}))], openai_handler, req)
    assert seen_openai[0]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "read_note",
                "description": "Read a saved note by id.",
                "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
        }
    ]

    # Anthropic tool serialization
    seen_anthropic: list[dict[str, Any]] = []

    def anthropic_handler(r: httpx.Request) -> httpx.Response:
        seen_anthropic.append(json.loads(r.content))
        return httpx.Response(200, json=ANTHROPIC_DATA)

    run_complete([anthropic(capabilities=frozenset({"text", "tools"}))], anthropic_handler, req)
    assert seen_anthropic[0]["tools"] == [
        {
            "name": "read_note",
            "description": "Read a saved note by id.",
            "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}},
        }
    ]


def test_tool_calls_and_parallel_tool_results_serialization() -> None:
    calls = (
        ToolProposal(call_id="c1", name="read_note", arguments={"id": "n1"}),
        ToolProposal(call_id="c2", name="list_notes", arguments={"limit": 5}),
    )
    messages = (
        ChatMessage("system", "Be accurate"),
        ChatMessage("user", "Check notes"),
        ChatMessage("assistant", "", tool_calls=calls),
        ChatMessage("tool", '{"ok":true}', tool_call_id="c1"),
        ChatMessage("tool", "not_found", tool_call_id="c2", is_error=True),
    )
    req = request(
        messages=messages,
        cloud_allowed=True,
        max_cost_usd=Decimal("1"),
        max_input_tokens=1000,
    )

    seen_openai: list[dict[str, Any]] = []

    def openai_handler(r: httpx.Request) -> httpx.Response:
        seen_openai.append(json.loads(r.content))
        return httpx.Response(200, json=OPENAI_DATA)

    run_complete([cloud()], openai_handler, req)
    assert seen_openai[0]["messages"] == [
        {"role": "system", "content": "Be accurate"},
        {"role": "user", "content": "Check notes"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "read_note", "arguments": '{"id":"n1"}'},
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "list_notes", "arguments": '{"limit":5}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": '{"ok":true}'},
        {"role": "tool", "tool_call_id": "c2", "content": "not_found"},
    ]

    seen_anthropic: list[dict[str, Any]] = []

    def anthropic_handler(r: httpx.Request) -> httpx.Response:
        seen_anthropic.append(json.loads(r.content))
        return httpx.Response(200, json=ANTHROPIC_DATA)

    run_complete([anthropic()], anthropic_handler, req)
    assert seen_anthropic[0]["system"] == "Be accurate"
    assert seen_anthropic[0]["messages"] == [
        {"role": "user", "content": "Check notes"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "c1", "name": "read_note", "input": {"id": "n1"}},
                {"type": "tool_use", "id": "c2", "name": "list_notes", "input": {"limit": 5}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": '{"ok":true}'},
                {
                    "type": "tool_result",
                    "tool_use_id": "c2",
                    "content": "not_found",
                    "is_error": True,
                },
            ],
        },
    ]


@pytest.mark.parametrize(
    "configured,returned,dialect",
    [
        ("gpt-4o", "gpt-4o-2024-08-06", Dialect.OPENAI_CHAT),
        ("gpt-4o-mini", "gpt-4o-mini-2024-07-18", Dialect.OPENAI_CHAT),
        ("gpt-3.5-turbo", "gpt-3.5-turbo-0125", Dialect.OPENAI_CHAT),
        ("claude-sonnet-4-5", "claude-sonnet-4-5-20250929", Dialect.ANTHROPIC_MESSAGES),
        ("claude-3-5-sonnet-latest", "claude-3-5-sonnet-20241022", Dialect.ANTHROPIC_MESSAGES),
    ],
)
def test_snapshot_model_ids_accepted_for_matching_aliases(
    configured: str, returned: str, dialect: Dialect
) -> None:
    if dialect == Dialect.OPENAI_CHAT:
        spec = cloud(model_id=configured)
        payload = {**OPENAI_DATA, "model": returned}
    else:
        spec = anthropic(model_id=configured)
        payload = {**ANTHROPIC_DATA, "model": returned}
    res = run_complete(
        [spec], respond_json(payload), request(cloud_allowed=True, max_cost_usd=Decimal("1"))
    )
    assert res.model_id == configured


@pytest.mark.parametrize(
    "configured,returned,dialect",
    [
        ("gpt-4o", "gpt-4o-mini", Dialect.OPENAI_CHAT),
        ("gpt-4o", "gpt-4o-mini-2024-07-18", Dialect.OPENAI_CHAT),
        ("gpt-4o-2024-08-06", "gpt-4o-2024-11-20", Dialect.OPENAI_CHAT),
        ("claude-sonnet-4-5", "claude-3-5-haiku-20241022", Dialect.ANTHROPIC_MESSAGES),
    ],
)
def test_cross_model_and_pinned_snapshot_drift_rejected(
    configured: str, returned: str, dialect: Dialect
) -> None:
    if dialect == Dialect.OPENAI_CHAT:
        spec = cloud(model_id=configured)
        payload = {**OPENAI_DATA, "model": returned}
    else:
        spec = anthropic(model_id=configured)
        payload = {**ANTHROPIC_DATA, "model": returned}
    with pytest.raises(GatewayError, match="model_mismatch"):
        run_complete(
            [spec], respond_json(payload), request(cloud_allowed=True, max_cost_usd=Decimal("1"))
        )


def test_decimal_enforced_and_anthropic_five_cent_preflight_boundary() -> None:
    with pytest.raises(ValueError, match="Invalid model token pricing"):
        cloud(input_usd_per_million=0.15)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Invalid USD spend ceiling"):
        request(max_cost_usd=0.05)  # type: ignore[arg-type]

    sonnet = anthropic(
        model_id="claude-sonnet-4-5",
        input_usd_per_million=Decimal("3.00"),
        output_usd_per_million=Decimal("15.00"),
    )
    ok_req = request(
        cloud_allowed=True,
        max_input_tokens=1200,
        max_output_tokens=1024,
        max_cost_usd=Decimal(str(0.05)),
    )
    assert (
        run_complete(
            [sonnet],
            respond_json({**ANTHROPIC_DATA, "model": "claude-sonnet-4-5-20250929"}),
            ok_req,
        ).content
        == "Answer ready"
    )

    over_req = request(
        cloud_allowed=True,
        max_input_tokens=1200,
        max_output_tokens=4096,
        max_cost_usd=Decimal(str(0.05)),
    )
    with pytest.raises(GatewayError, match="no_eligible_provider"):
        run_complete([sonnet], lambda _: pytest.fail("Should fail preflight"), over_req)


def test_chat_message_and_tool_definition_validation() -> None:
    with pytest.raises(ValueError, match="valid tool_call_id"):
        ChatMessage("tool", "output", tool_call_id=None)
    with pytest.raises(ValueError, match="only valid for tool messages"):
        ChatMessage("user", "hi", tool_call_id="c1")
    with pytest.raises(ValueError, match="only valid for tool messages"):
        ChatMessage("assistant", "hi", is_error=True)
    with pytest.raises(ValueError, match="Only assistant messages"):
        ChatMessage("user", "hi", tool_calls=(ToolProposal("c1", "fn", {}),))
    with pytest.raises(ValueError, match="Duplicate tool definition name"):
        request(
            max_input_tokens=500,
            tools=(ToolDefinition("t1", "a"), ToolDefinition("t1", "b")),
        )
    many_tools = tuple(ToolDefinition(f"t_{idx}", "desc") for idx in range(33))
    with pytest.raises(ValueError, match="At most 32"):
        request(max_input_tokens=5000, tools=many_tools)


@pytest.mark.parametrize(
    "configured,returned,dialect",
    [
        ("gpt-4o", "gpt-4o-2024-99-99", Dialect.OPENAI_CHAT),
        ("gpt-4o", "gpt-4o-2024-02-31", Dialect.OPENAI_CHAT),
        ("gpt-4o", "gpt-4o-2023-02-29", Dialect.OPENAI_CHAT),
        ("gpt-4o", "gpt-4o-20249999", Dialect.OPENAI_CHAT),
        ("gpt-4o", "gpt-4o-0230", Dialect.OPENAI_CHAT),
        ("gpt-4o", "gpt-4o-0231", Dialect.OPENAI_CHAT),
        ("gpt-4o", "gpt-4o-9999", Dialect.OPENAI_CHAT),
        ("gpt-4o", "gpt-4o-0000", Dialect.OPENAI_CHAT),
        ("claude-sonnet-4-5", "claude-sonnet-4-5-20240230", Dialect.ANTHROPIC_MESSAGES),
        ("claude-3-5-sonnet-latest", "claude-3-5-sonnet-2024-99-99", Dialect.ANTHROPIC_MESSAGES),
    ],
)
def test_invalid_calendar_date_snapshots_rejected(
    configured: str, returned: str, dialect: Dialect
) -> None:
    if dialect == Dialect.OPENAI_CHAT:
        spec = cloud(model_id=configured)
        payload = {**OPENAI_DATA, "model": returned}
    else:
        spec = anthropic(model_id=configured)
        payload = {**ANTHROPIC_DATA, "model": returned}
    with pytest.raises(GatewayError, match="model_mismatch"):
        run_complete(
            [spec], respond_json(payload), request(cloud_allowed=True, max_cost_usd=Decimal("1"))
        )


@pytest.mark.parametrize(
    "configured,returned,dialect",
    [
        ("gpt-4o", "gpt-4o-2024-02-29", Dialect.OPENAI_CHAT),
        ("gpt-4o", "gpt-4o-0229", Dialect.OPENAI_CHAT),
        ("claude-sonnet-4-5", "claude-sonnet-4-5-20240229", Dialect.ANTHROPIC_MESSAGES),
    ],
)
def test_valid_leap_day_snapshots_accepted(
    configured: str, returned: str, dialect: Dialect
) -> None:
    if dialect == Dialect.OPENAI_CHAT:
        spec = cloud(model_id=configured)
        payload = {**OPENAI_DATA, "model": returned}
    else:
        spec = anthropic(model_id=configured)
        payload = {**ANTHROPIC_DATA, "model": returned}
    res = run_complete(
        [spec], respond_json(payload), request(cloud_allowed=True, max_cost_usd=Decimal("1"))
    )
    assert res.model_id == configured


def test_anthropic_tool_then_user_merges_without_consecutive_user_roles() -> None:
    calls = (ToolProposal(call_id="c1", name="read_note", arguments={"id": "n1"}),)
    messages = (
        ChatMessage("system", "Be accurate"),
        ChatMessage("user", "start"),
        ChatMessage("assistant", "", tool_calls=calls),
        ChatMessage("tool", "res1", tool_call_id="c1"),
        ChatMessage("user", "follow-up"),
    )
    req = request(
        messages=messages,
        cloud_allowed=True,
        max_cost_usd=Decimal("1"),
        max_input_tokens=1000,
    )
    seen: list[dict[str, Any]] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(json.loads(r.content))
        return httpx.Response(200, json=ANTHROPIC_DATA)

    run_complete([anthropic()], handler, req)
    wire_msgs = seen[0]["messages"]
    assert [m["role"] for m in wire_msgs] == ["user", "assistant", "user"]
    assert wire_msgs[0] == {"role": "user", "content": "start"}
    assert wire_msgs[2] == {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "res1"},
            {"type": "text", "text": "follow-up"},
        ],
    }

    # Empty trailing user message after tool keeps non-empty block list with no empty text block.
    seen.clear()
    empty_follow = replace(
        req,
        messages=(
            ChatMessage("user", "start"),
            ChatMessage("assistant", "", tool_calls=calls),
            ChatMessage("tool", "res1", tool_call_id="c1"),
            ChatMessage("user", ""),
        ),
    )
    run_complete([anthropic()], handler, empty_follow)
    assert [m["role"] for m in seen[0]["messages"]] == ["user", "assistant", "user"]
    assert seen[0]["messages"][2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "res1"}],
    }


def test_normalize_tool_rejects_control_chars_non_finite_and_oversized_utf8() -> None:
    for bad_id in ("call\n1", "call\r1", "call\x001"):
        bad_id_payload = {
            **OPENAI_DATA,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": bad_id, "function": {"name": "read_note", "arguments": "{}"}}
                        ],
                    }
                }
            ],
        }
        with pytest.raises(GatewayError, match="invalid_tool_proposal"):
            run_complete([local()], respond_json(bad_id_payload))

    for bad_const in ("NaN", "Infinity", "-Infinity"):
        nan_payload = {
            **OPENAI_DATA,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "function": {
                                    "name": "read_note",
                                    "arguments": f'{{"x":{bad_const}}}',
                                },
                            }
                        ],
                    }
                }
            ],
        }
        with pytest.raises(GatewayError, match="invalid_tool_proposal"):
            run_complete([local()], respond_json(nan_payload))

    # Anthropic dict with non-finite float
    from max_core.gateway.normalize import normalize_anthropic

    anthropic_nan = {
        **ANTHROPIC_DATA,
        "content": [
            {"type": "tool_use", "id": "t1", "name": "read_note", "input": {"x": float("nan")}}
        ],
    }
    with pytest.raises(GatewayError, match="invalid_tool_proposal"):
        normalize_anthropic(anthropic_nan, "req-1", anthropic())

    # 17,000 'é' chars -> < 32,000 chars, > 32,000 UTF-8 bytes
    utf8_args = json.dumps({"s": "é" * 17_000}, separators=(",", ":"), ensure_ascii=False)
    assert len(utf8_args) < 32_000
    assert len(utf8_args.encode("utf-8")) > 32_000
    utf8_payload = {
        **OPENAI_DATA,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "read_note", "arguments": utf8_args}}
                    ],
                }
            }
        ],
    }
    with pytest.raises(GatewayError, match="invalid_tool_proposal"):
        run_complete([local()], respond_json(utf8_payload))


def _make_deep_dict(depth: int, leaf: dict[str, Any] | None = None) -> dict[str, Any]:
    node: dict[str, Any] = dict(leaf) if leaf is not None else {"ok": 1}
    for _ in range(depth):
        node = {"a": node}
    return node


def test_deep_recursion_hardening_in_contracts_normalize_and_adapter() -> None:
    from max_core.gateway.contracts import request_envelope_bytes
    from max_core.gateway.normalize import normalize_anthropic, normalize_openai

    deep_3000 = _make_deep_dict(3000)

    with pytest.raises(ValueError, match="JSON-serializable"):
        ToolDefinition("t1", "desc", deep_3000)

    with pytest.raises(ValueError, match="JSON-serializable"):
        ChatMessage("assistant", "", tool_calls=(ToolProposal("c1", "t1", deep_3000),))

    # Mutated deep dicts in request_envelope_bytes and HTTPAdapter.complete/stream
    mutated_call = ToolProposal("c1", "read_note", {})
    mutated_msg = ChatMessage("assistant", "", tool_calls=(mutated_call,))
    object.__setattr__(mutated_call, "arguments", deep_3000)
    with pytest.raises(ValueError, match="JSON-serializable"):
        request_envelope_bytes((mutated_msg,))

    # Bypass GatewayRequest.__post_init__ envelope check by mutating after construction
    clean_call = ToolProposal("c1", "read_note", {})
    req_for_adapter = replace(
        request(max_input_tokens=10_000),
        messages=(
            ChatMessage("user", "hi"),
            ChatMessage("assistant", "", tool_calls=(clean_call,)),
            ChatMessage("tool", "ok", tool_call_id="c1"),
        ),
    )
    object.__setattr__(clean_call, "arguments", deep_3000)

    async def check_adapter_recursion() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("Should fail before transport"))
        ) as client:
            adapter = HTTPAdapter(client)
            with pytest.raises(GatewayError, match="invalid_request"):
                await adapter.complete(local(), req_for_adapter)
            with pytest.raises(GatewayError, match="invalid_request"):
                async for _ in adapter.stream(local(), req_for_adapter):
                    pass

        for bad_body in ("[" * 5000 + "]" * 5000, '{"a":' * 5000 + "1" + "}" * 5000):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(lambda _r, b=bad_body: httpx.Response(200, text=b))
            ) as client:
                with pytest.raises(GatewayError, match="malformed_provider_response"):
                    await HTTPAdapter(client).complete(local(), request())

    asyncio.run(check_adapter_recursion())

    for bad_args in ("[" * 5000, '{"a":' * 5000):
        openai_deep = {
            **OPENAI_DATA,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "c1", "function": {"name": "read_note", "arguments": bad_args}}
                        ],
                    }
                }
            ],
        }
        with pytest.raises(GatewayError, match="invalid_tool_proposal"):
            normalize_openai(openai_deep, "req-1", local())

    for depth in (2985, 3000):
        anthropic_deep = {
            **ANTHROPIC_DATA,
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "read_note",
                    "input": _make_deep_dict(depth),
                }
            ],
        }
        with pytest.raises(GatewayError, match="invalid_tool_proposal"):
            normalize_anthropic(anthropic_deep, "req-1", anthropic())


def test_lone_surrogates_rejected_in_contracts_normalize_and_adapter() -> None:
    from max_core.gateway.normalize import normalize_anthropic, normalize_openai

    with pytest.raises(ValueError, match="UTF-8"):
        ChatMessage("user", "hello \ud800 world")
    with pytest.raises(ValueError, match="tool_call_id"):
        ChatMessage("tool", "ok", tool_call_id="call_\ud800")
    with pytest.raises(ValueError, match="call_id"):
        ChatMessage("assistant", "", tool_calls=(ToolProposal("call_\udfff", "t1", {}),))
    with pytest.raises(ValueError, match="JSON-serializable"):
        ChatMessage("assistant", "", tool_calls=(ToolProposal("c1", "t1", {"x": "\ud800"}),))
    with pytest.raises(ValueError, match="UTF-8"):
        ToolDefinition("t1", "bad \ud800 desc")
    with pytest.raises(ValueError, match="JSON-serializable"):
        ToolDefinition("t1", "desc", {"x": "\udfff"})
    with pytest.raises(ValueError, match="API key"):
        ModelSpec(
            provider_id="local",
            model_id="demo-chat",
            dialect=Dialect.OPENAI_CHAT,
            endpoint="http://127.0.0.1:8080/v1/chat",
            is_local=True,
            context_tokens=4096,
            api_key="bad_\ud800_key",
        )

    # normalize_openai / normalize_anthropic
    with pytest.raises(GatewayError, match="malformed_provider_response"):
        normalize_openai(
            {
                **OPENAI_DATA,
                "choices": [{"message": {"role": "assistant", "content": "hi \ud800"}}],
            },
            "r1",
            local(),
        )
    with pytest.raises(GatewayError, match="malformed_provider_response"):
        normalize_openai(
            {
                **OPENAI_DATA,
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop_\ud800",
                    }
                ],
            },
            "r1",
            local(),
        )
    with pytest.raises(GatewayError, match="invalid_tool_proposal"):
        normalize_openai(
            {
                **OPENAI_DATA,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c_\ud800",
                                    "function": {"name": "read_note", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ],
            },
            "r1",
            local(),
        )
    with pytest.raises(GatewayError, match="invalid_tool_proposal"):
        normalize_openai(
            {
                **OPENAI_DATA,
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "function": {
                                        "name": "read_note",
                                        "arguments": '{"x":"\\ud800"}',
                                    },
                                }
                            ],
                        }
                    }
                ],
            },
            "r1",
            local(),
        )

    with pytest.raises(GatewayError, match="malformed_provider_response"):
        normalize_anthropic(
            {**ANTHROPIC_DATA, "content": [{"type": "text", "text": "hi \udfff"}]},
            "r1",
            anthropic(),
        )
    with pytest.raises(GatewayError, match="malformed_provider_response"):
        normalize_anthropic({**ANTHROPIC_DATA, "stop_reason": "end_\ud800"}, "r1", anthropic())
    with pytest.raises(GatewayError, match="invalid_tool_proposal"):
        normalize_anthropic(
            {
                **ANTHROPIC_DATA,
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "read_note", "input": {"x": "\ud800"}}
                ],
            },
            "r1",
            anthropic(),
        )

    # HTTPAdapter with mutated surrogate arguments/parameters
    clean_tool = ToolDefinition("read_note", "Read", {"type": "object"})
    req_mut_tool = replace(
        request(max_input_tokens=1000),
        tools=(clean_tool,),
    )
    object.__setattr__(clean_tool, "parameters", {"type": "object", "bad": "\ud800"})

    async def check_adapter_surrogate() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: pytest.fail("Surrogate reached transport"))
        ) as client:
            adapter = HTTPAdapter(client)
            spec = local(capabilities=frozenset({"text", "tools"}))
            with pytest.raises(GatewayError, match="invalid_request"):
                await adapter.complete(spec, req_mut_tool)
            with pytest.raises(GatewayError, match="invalid_request"):
                async for _ in adapter.stream(spec, req_mut_tool):
                    pass

    asyncio.run(check_adapter_surrogate())
