"""Stage A gateway projection tests. No provider traffic."""

from __future__ import annotations

from dataclasses import asdict, replace
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
    AvailabilityObservation,
    AvailabilityState,
    DiscoveredRoute,
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
    assert result == ()


def test_unreviewed_route_is_excluded_without_execution_error() -> None:
    gateway = Gateway(
        [spec("provider-A", InferenceDestination.VERIFIED_LOCAL)],
        HTTPAdapter(httpx.AsyncClient()),
    )
    identity, policy = binding("provider-A", "route-A")
    unreviewed = replace(policy, approval=ApprovalState.UNREVIEWED)
    assert gateway.selection_metadata(
        [(identity, unreviewed)], catalogue_revision="cat-1"
    ) == ()


def test_disabled_route_is_excluded_without_execution_error() -> None:
    gateway = Gateway(
        [spec("provider-A", InferenceDestination.VERIFIED_LOCAL)],
        HTTPAdapter(httpx.AsyncClient()),
    )
    identity, policy = binding("provider-A", "route-A")
    disabled = replace(policy, approval=ApprovalState.DISABLED)
    assert gateway.selection_metadata(
        [(identity, disabled)], catalogue_revision="cat-1"
    ) == ()


def test_approved_local_route_remains_candidate() -> None:
    gateway = Gateway(
        [spec("provider-A", InferenceDestination.VERIFIED_LOCAL)],
        HTTPAdapter(httpx.AsyncClient()),
    )
    candidate = gateway.selection_metadata(
        [binding("provider-A", "route-A")], catalogue_revision="cat-1"
    )
    assert len(candidate) == 1
    assert candidate[0].approval == ApprovalState.APPROVED
    assert candidate[0].inference_destination == InferenceDestination.VERIFIED_LOCAL


def test_approved_cloud_route_is_metadata_not_cloud_execution_permission() -> None:
    gateway = Gateway(
        [spec("provider-A", InferenceDestination.CLOUD)],
        HTTPAdapter(httpx.AsyncClient()),
    )
    identity, policy = binding("provider-A", "route-A")
    policy = replace(policy, inference_destination=InferenceDestination.CLOUD)
    candidate = gateway.selection_metadata(
        [(identity, policy)], catalogue_revision="cat-1"
    )
    assert len(candidate) == 1
    assert candidate[0].inference_destination == InferenceDestination.CLOUD
    # An approved selection candidate must still satisfy the original
    # Gateway.eligible() privacy, cloud-opt-in and request-budget checks.
    req = GatewayRequest(
        request_id="no-cloud-approval",
        messages=(ChatMessage("user", "hi"),),
        privacy=Privacy.PUBLIC,
        max_input_tokens=200,
        max_output_tokens=50,
    )
    assert gateway.eligible(req) == ()


def test_gateway_ready_discovery_does_not_approve_candidate() -> None:
    gateway = Gateway(
        [spec("provider-A", InferenceDestination.VERIFIED_LOCAL)],
        HTTPAdapter(httpx.AsyncClient()),
    )
    identity, policy = binding("provider-A", "route-A")
    discovered = DiscoveredRoute(
        identity,
        availability=AvailabilityObservation(state=AvailabilityState.READY),
    )
    assert discovered.availability.state == AvailabilityState.READY
    policy = replace(policy, approval=ApprovalState.UNREVIEWED)
    assert gateway.selection_metadata(
        [(identity, policy)], catalogue_revision="cat-1"
    ) == ()


def test_policy_destination_mismatch_rejected_even_when_unreviewed() -> None:
    gateway = Gateway(
        [spec("provider-A", InferenceDestination.VERIFIED_LOCAL)],
        HTTPAdapter(httpx.AsyncClient()),
    )
    identity, policy = binding("provider-A", "route-A")
    mismatch = replace(
        policy,
        approval=ApprovalState.UNREVIEWED,
        inference_destination=InferenceDestination.CLOUD,
    )
    with pytest.raises(ValueError, match="destination"):
        gateway.selection_metadata([(identity, mismatch)], catalogue_revision="cat-1")


def test_empty_gateway_returns_empty_candidates_not_gateway_error() -> None:
    gateway = Gateway([], HTTPAdapter(httpx.AsyncClient()))
    assert gateway.selection_metadata([], catalogue_revision="cat-1") == ()
