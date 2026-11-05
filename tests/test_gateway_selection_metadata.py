"""Stage A gateway projection tests. No provider traffic."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

import httpx
import pytest

from max_core.gateway import (
    ChatMessage,
    Dialect,
    Gateway,
    GatewayRequest,
    HTTPAdapter,
    InferenceDestination,
    ModelSpec,
    Privacy,
)
from max_core.gateway.catalogue import (
    ApprovalState,
    EvidenceSource,
    ModelIdentity,
    RouteIdentity,
    TrustedRoutePolicy,
)


def spec(provider: str, destination: InferenceDestination) -> ModelSpec:
    return ModelSpec(
        provider_id=provider,
        model_id="same-model",
        dialect=Dialect.OPENAI_CHAT,
        endpoint="http://127.0.0.1:13000/v1/chat/completions",
        is_local=True,
        context_tokens=4096,
        inference_destination=destination,
        api_key="SENSITIVE_FAKE_KEY_NOT_FOR_SELECTION",
        input_usd_per_million=Decimal("0"),
        output_usd_per_million=Decimal("0"),
    )


def binding(provider: str, route_id: str, gateway_id: str = "gateway-A") -> tuple[
    RouteIdentity, TrustedRoutePolicy
]:
    identity = RouteIdentity(
        ModelIdentity(gateway_id, provider, "same-model"), route_id, Dialect.OPENAI_CHAT
    )
    policy = TrustedRoutePolicy(
        identity=identity,
        approval=ApprovalState.APPROVED,
        inference_destination=InferenceDestination.VERIFIED_LOCAL,
        evidence_source=EvidenceSource.OWNER_REVIEWED,
        policy_revision="approved-v1",
    )
    return identity, policy


def test_gateway_safe_metadata_never_copies_endpoint_or_credential() -> None:
    gateway = Gateway(
        [spec("provider-A", InferenceDestination.VERIFIED_LOCAL)],
        HTTPAdapter(httpx.AsyncClient()),
    )
    view = gateway.selection_metadata(
        [binding("provider-A", "route-A")], catalogue_revision="cat-1"
    )
    assert len(view) == 1
    assert view[0].identity.model.provider_id == "provider-A"
    assert view[0].catalogue_revision == "cat-1"
    assert "SENSITIVE_FAKE_KEY" not in str(asdict(view[0]))
    assert "13000" not in str(asdict(view[0]))
    assert "endpoint" not in asdict(view[0])


def test_same_model_names_require_distinct_route_bindings() -> None:
    gateway = Gateway(
        [
            spec("provider-A", InferenceDestination.VERIFIED_LOCAL),
            spec("provider-B", InferenceDestination.VERIFIED_LOCAL),
        ],
        HTTPAdapter(httpx.AsyncClient()),
    )
    bindings = [binding("provider-A", "route-A"), binding("provider-B", "route-B")]
    view = gateway.selection_metadata(bindings, catalogue_revision="cat-1")
    assert len({v.identity for v in view}) == 2
    with pytest.raises(ValueError, match="Duplicate or ambiguous"):
        gateway.selection_metadata(
            [binding("provider-A", "same-route"), binding("provider-B", "same-route")],
            catalogue_revision="cat-1",
        )
    with pytest.raises(ValueError, match="explicit route bindings"):
        gateway.selection_metadata([bindings[0]], catalogue_revision="cat-1")


def test_route_mismatch_cannot_create_authorized_metadata() -> None:
    gateway = Gateway(
        [spec("provider-A", InferenceDestination.VERIFIED_LOCAL)],
        HTTPAdapter(httpx.AsyncClient()),
    )
    with pytest.raises(ValueError, match="no matching configured model"):
        gateway.selection_metadata(
            [binding("not-configured", "route-X")], catalogue_revision="cat-1"
        )


def test_unknown_destination_still_fails_central_gateway_admission() -> None:
    gateway = Gateway(
        [spec("provider-A", InferenceDestination.UNKNOWN)],
        HTTPAdapter(httpx.AsyncClient()),
    )
    req = GatewayRequest(
        request_id="stage-a-unknown",
        messages=(ChatMessage("user", "hi"),),
        privacy=Privacy.PUBLIC,
        max_input_tokens=200,
        max_output_tokens=50,
        cloud_allowed=True,
        max_cost_usd=Decimal("100"),
    )
    assert gateway.eligible(req) == ()
    identity, _ = binding("provider-A", "route-A")
    result = gateway.selection_metadata(
        [(identity, TrustedRoutePolicy(identity))],
        catalogue_revision="cat-1",
    )
    assert result[0].inference_destination == InferenceDestination.UNKNOWN
