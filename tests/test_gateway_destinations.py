"""Mock-only security regressions for transport locality vs ultimate inference egress.

No real provider, credentials, network service, or model is contacted.
"""

from __future__ import annotations

import asyncio
import logging
import traceback
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from typing import Any

import httpx
import pytest

from max_core.gateway import (
    ChatMessage,
    Gateway,
    GatewayError,
    GatewayRequest,
    GatewayResponse,
    HTTPAdapter,
    InferenceDestination,
    ModelSpec,
    Privacy,
    StreamEvent,
    TokenUsage,
    ToolProposal,
)
from tests.test_gateway import OPENAI_DATA, cloud, local, request, run_complete


def proxy_cloud(**overrides: Any) -> ModelSpec:
    """Cloud-forwarding proxy on localhost: endpoint is not inference location."""
    return replace(
        cloud(
            provider_id="local-cloud-proxy",
            endpoint="http://127.0.0.1:14001/v1/chat/completions",
            is_local=True,
        ),
        **overrides,
    )


def unknown_proxy(**overrides: Any) -> ModelSpec:
    return replace(
        local(inference_destination=InferenceDestination.UNKNOWN),
        **overrides,
    )


def test_loopback_cloud_proxy_requires_cloud_authorization() -> None:
    proxy = proxy_cloud()
    assert proxy.is_local is True
    assert proxy.inference_destination == InferenceDestination.CLOUD
    for req in (request(), request(max_cost_usd=Decimal("10"))):
        with pytest.raises(GatewayError, match="no_eligible_provider"):
            run_complete([proxy], lambda _: pytest.fail("cloud proxy contacted"), req)


def test_loopback_cloud_proxy_allowed_only_with_cloud_and_cost_permission() -> None:
    proxy = proxy_cloud()
    seen: list[httpx.Request] = []

    def transport(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=OPENAI_DATA)

    result = run_complete(
        [proxy], transport, request(cloud_allowed=True, max_cost_usd=Decimal("0.01"))
    )
    assert result.content == "Hello"
    assert len(seen) == 1
    assert seen[0].url.host == "127.0.0.1"
    assert seen[0].url.port == 14001


@pytest.mark.parametrize("destination", [unknown_proxy(), cloud(inference_destination=InferenceDestination.UNKNOWN)])
@pytest.mark.parametrize("privacy", [Privacy.PUBLIC, Privacy.INTERNAL, Privacy.CONFIDENTIAL, Privacy.RESTRICTED])
def test_unknown_inference_denied_even_with_cloud_opt_in(
    destination: ModelSpec, privacy: Privacy
) -> None:
    with pytest.raises(GatewayError, match="no_eligible_provider"):
        run_complete(
            [destination],
            lambda _: pytest.fail("unknown inference destination contacted"),
            request(privacy=privacy, cloud_allowed=True, max_cost_usd=Decimal("100")),
        )


@pytest.mark.parametrize("privacy", [Privacy.CONFIDENTIAL, Privacy.RESTRICTED])
def test_sensitive_data_denied_to_loopback_cloud_proxy(privacy: Privacy) -> None:
    with pytest.raises(GatewayError, match="no_eligible_provider"):
        run_complete(
            [proxy_cloud()],
            lambda _: pytest.fail("sensitive data forwarded to cloud"),
            request(privacy=privacy, cloud_allowed=True, max_cost_usd=Decimal("100")),
        )


@pytest.mark.parametrize(
    "incomplete",
    [
        {},
        {"max_cost_usd": Decimal("0.00000001")},
        {"max_cost_usd": Decimal("1"), "local_only": True},
    ],
)
def test_cloud_proxy_requires_valid_cost_and_local_only_permissions(incomplete: dict[str, Any]) -> None:
    with pytest.raises(GatewayError, match="no_eligible_provider"):
        run_complete(
            [proxy_cloud()],
            lambda _: pytest.fail("cost or local-only gate bypassed"),
            request(cloud_allowed=True, **incomplete),
        )


@pytest.mark.parametrize(
    "input_rate,output_rate",
    [
        (None, Decimal("1")),
        (Decimal("1"), None),
        (None, None),
    ],
)
def test_cloud_proxy_with_unknown_pricing_cannot_send(
    input_rate: Decimal | None, output_rate: Decimal | None
) -> None:
    proxy = proxy_cloud(
        input_usd_per_million=input_rate, output_usd_per_million=output_rate
    )
    with pytest.raises(GatewayError, match="no_eligible_provider"):
        run_complete(
            [proxy],
            lambda _: pytest.fail("unknown-price cloud proxy contacted"),
            request(cloud_allowed=True, max_cost_usd=Decimal("100")),
        )


def test_unknown_destination_with_zero_prices_is_not_free_local() -> None:
    with pytest.raises(GatewayError, match="no_eligible_provider"):
        run_complete(
            [unknown_proxy()],
            lambda _: pytest.fail("unknown proxy contacted"),
            request(cloud_allowed=True, max_cost_usd=Decimal("100")),
        )


def test_verified_local_inference_remains_available_for_sensitive_data() -> None:
    result = run_complete(
        [local(input_usd_per_million=None, output_usd_per_million=None)],
        lambda _: httpx.Response(200, json=OPENAI_DATA),
        request(privacy=Privacy.RESTRICTED, local_only=True),
    )
    assert result.provider_id == "local"


def test_non_loopback_verified_local_claim_rejected() -> None:
    with pytest.raises(ValueError, match="Verified local inference requires"):
        cloud(inference_destination=InferenceDestination.VERIFIED_LOCAL)


@pytest.mark.parametrize("invalid", ["local", "cloud", None, False, 1])
def test_inference_destination_requires_explicit_enum(invalid: Any) -> None:
    with pytest.raises(ValueError, match="Invalid inference destination"):
        local(inference_destination=invalid)


@pytest.mark.parametrize(
    "changes",
    [
        {"cloud_allowed": "false"},
        {"cloud_allowed": 1},
        {"local_only": "false"},
        {"allow_fallback": "true"},
    ],
)
def test_cloud_and_fallback_authorization_flags_are_exact_booleans(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValueError, match="authorization flags"):
        request(**changes)


def test_loopback_cloud_proxy_cannot_be_fallback_for_restricted_data() -> None:
    seen: list[int | None] = []

    def transport(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.port)
        return httpx.Response(429)

    with pytest.raises(GatewayError, match="rate_limited"):
        run_complete(
            [local(priority=0), proxy_cloud(priority=10)],
            transport,
            request(
                cloud_allowed=True,
                max_cost_usd=Decimal("10"),
                allow_fallback=True,
                privacy=Privacy.RESTRICTED,
            ),
        )
    assert seen == [14000]


def test_fallback_never_sends_to_unknown_or_unauthorized_cloud_proxy() -> None:
    for other, req in (
        (unknown_proxy(provider_id="unknown", priority=10), request(allow_fallback=True)),
        (proxy_cloud(priority=10), request(allow_fallback=True)),
    ):
        seen: list[int | None] = []

        def transport(msg: httpx.Request) -> httpx.Response:
            seen.append(msg.url.port)
            return httpx.Response(503)

        with pytest.raises(GatewayError, match="provider_unavailable"):
            run_complete([local(priority=0), other], transport, req)
        assert seen == [14000]


def test_approved_fallback_rechecks_cloud_eligibility() -> None:
    seen: list[int | None] = []

    def transport(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.port)
        if req.url.port == 14000:
            return httpx.Response(429)
        assert req.url.port == 14001
        return httpx.Response(200, json=OPENAI_DATA)

    result = run_complete(
        [local(priority=0), proxy_cloud(priority=10)],
        transport,
        request(
            allow_fallback=True,
            cloud_allowed=True,
            max_cost_usd=Decimal("1"),
            privacy=Privacy.PUBLIC,
        ),
    )
    assert seen == [14000, 14001]
    assert result.provider_id == "local-cloud-proxy"


def test_gateway_request_and_response_repr_hide_prompts_and_tool_arguments() -> None:
    marker = "SENSITIVE_PROMPT_DO_NOT_LOG"
    tool_marker = "SENSITIVE_TOOL_ARGS_DO_NOT_LOG"
    req = GatewayRequest(
        request_id="sensitive-req-001",
        messages=(ChatMessage("user", marker),),
        privacy=Privacy.PUBLIC,
        max_input_tokens=100,
        max_output_tokens=5,
    )
    proposal = ToolProposal(
        call_id="call1", name="search", arguments={"token": tool_marker}
    )
    result = GatewayResponse(
        request_id="sensitive-req-001",
        provider_id="fake",
        model_id="demo",
        content=marker,
        tool_proposals=(proposal,),
        finish_reason="stop",
        usage=TokenUsage(1, 1),
        estimated_cost_usd=Decimal("0"),
    )
    event = StreamEvent(
        sequence=0,
        request_id="sensitive-req-001",
        provider_id="fake",
        model_id="demo",
        kind="content_delta",
        text=marker,
        tool_proposal=proposal,
    )
    for value in (req.messages[0], req, proposal, result, event):
        assert marker not in repr(value)
        assert tool_marker not in repr(value)
        assert marker not in str(value)
        assert tool_marker not in str(value)

    sink = StringIO()
    handler = logging.StreamHandler(sink)
    log = logging.getLogger("max.gateway.test.redaction")
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    try:
        log.info("gateway objects: %r %r %r", req, result, proposal)
        handler.flush()
        assert marker not in sink.getvalue()
        assert tool_marker not in sink.getvalue()
    finally:
        log.removeHandler(handler)
        handler.close()


def test_model_spec_repr_never_reveals_dummy_credential() -> None:
    marker = "SENSITIVE_DUMMY_CREDENTIAL"
    model = proxy_cloud(api_key=marker)
    assert marker not in repr(model)
    assert marker not in str(model)


@pytest.mark.parametrize("failure", ["timeout", "request", "invalid_json"])
def test_provider_failure_path_suppresses_sensitive_exception_chain(
    failure: str,
) -> None:
    marker = "SENSITIVE_UPSTREAM_ERROR_AND_PROMPT_DO_NOT_LOG"

    def transport(req: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout(marker, request=req)
        if failure == "request":
            raise httpx.ConnectError(marker, request=req)
        return httpx.Response(200, text="{not-json:" + marker)

    with pytest.raises(GatewayError) as captured:
        run_complete(
            [proxy_cloud(api_key="SENSITIVE_DUMMY_CREDENTIAL")],
            transport,
            request(cloud_allowed=True, max_cost_usd=Decimal("1")),
        )
    assert captured.value.__suppress_context__ is True
    public_traceback = "".join(traceback.format_exception(captured.value))
    for sensitive in (marker, "SENSITIVE_DUMMY_CREDENTIAL"):
        assert sensitive not in str(captured.value)
        assert sensitive not in repr(captured.value)
        assert sensitive not in public_traceback
    assert captured.value.code in {
        "timeout", "provider_unavailable", "malformed_provider_response"
    }


def test_stream_transport_error_traceback_is_sanitized() -> None:
    marker = "SENSITIVE_STREAM_EXCEPTION_DO_NOT_LOG"

    async def call() -> None:
        def transport(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(marker, request=req)

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            gateway = Gateway(
                [proxy_cloud()],
                HTTPAdapter(client),
            )
            async for _ in gateway.stream(
                request(cloud_allowed=True, max_cost_usd=Decimal("1"))
            ):
                pass

    with pytest.raises(GatewayError) as captured:
        asyncio.run(call())
    assert captured.value.code == "stream_interrupted"
    assert captured.value.__suppress_context__ is True
    assert marker not in "".join(traceback.format_exception(captured.value))
