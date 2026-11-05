"""Offline Stage A contracts and credential-isolation tests."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

import pytest

from max_core.gateway.catalogue import (
    ApprovalState,
    AvailabilityObservation,
    AvailabilityState,
    CapabilityEvidence,
    CapabilityMetadata,
    CapabilitySupport,
    CatalogueSnapshot,
    DiscoveredRoute,
    EvidenceSource,
    ModelIdentity,
    ModelMetadata,
    PricingProvenance,
    ProviderMetadata,
    RouteIdentity,
    TrustedRoutePolicy,
    project_model_spec,
)
from max_core.gateway.contracts import Dialect, InferenceDestination, ModelSpec


def route(
    provider: str = "new-provider", model: str = "model/1", route_id: str = "route-a"
) -> RouteIdentity:
    return RouteIdentity(
        ModelIdentity("gateway-dev", provider, model), route_id, Dialect.OPENAI_CHAT
    )


def model(**changes: object) -> ModelSpec:
    values = dict(
        provider_id="new-provider",
        model_id="model/1",
        dialect=Dialect.OPENAI_CHAT,
        endpoint="http://127.0.0.1:18000/v1/chat/completions",
        is_local=True,
        context_tokens=4096,
        inference_destination=InferenceDestination.VERIFIED_LOCAL,
        api_key="DUMMY_SECRET_NEVER_COPY_123456789",
        capabilities=frozenset({"text", "tools"}),
        input_usd_per_million=Decimal("0"),
        output_usd_per_million=Decimal("0"),
    )
    values.update(changes)
    return ModelSpec(**values)


def approved(identity: RouteIdentity | None = None) -> TrustedRoutePolicy:
    return TrustedRoutePolicy(
        identity=identity or route(),
        approval=ApprovalState.APPROVED,
        inference_destination=InferenceDestination.VERIFIED_LOCAL,
        evidence_source=EvidenceSource.OWNER_REVIEWED,
        policy_revision="owner-reviewed-1",
    )


def test_capabilities_are_extensible_unknown_by_default() -> None:
    claims = CapabilityMetadata(
        claims=(
            CapabilityEvidence(
                "vision", CapabilitySupport.SUPPORTED, EvidenceSource.GATEWAY_REPORTED
            ),
            CapabilityEvidence("structured_output"),
            CapabilityEvidence(
                "reasoning.extended", CapabilitySupport.SUPPORTED,
                EvidenceSource.PROTOCOL_TESTED,
            ),
        )
    )
    assert claims.state("audio") == CapabilitySupport.UNKNOWN
    assert CapabilityMetadata().state("text") == CapabilitySupport.UNKNOWN
    assert claims.state("vision") == CapabilitySupport.SUPPORTED
    assert not claims.verified_supported("vision")
    assert claims.verified_supported("reasoning.extended")
    assert not claims.verified_supported("structured_output")
    with pytest.raises(ValueError, match="Duplicate capability"):
        CapabilityMetadata(claims=(CapabilityEvidence("text"), CapabilityEvidence("text")))
    with pytest.raises(ValueError, match="capability name"):
        CapabilityEvidence("Bearer leaked-secret")
    with pytest.raises(ValueError, match="schema version"):
        CapabilityMetadata(schema_version=2)


def test_any_provider_count_and_same_model_name_are_supported() -> None:
    providers = tuple(ProviderMetadata("gateway-dev", f"provider-{i}") for i in range(23))
    models = tuple(
        ModelMetadata(ModelIdentity("gateway-dev", p.provider_id, "same-model"))
        for p in providers
    )
    routes = tuple(
        DiscoveredRoute(route(p.provider_id, "same-model", f"route-{i}"))
        for i, p in enumerate(providers)
    )
    snapshot = CatalogueSnapshot("gateway-dev", "revision-23", providers, models, routes)
    assert len(snapshot.routes) == 23
    assert len({r.identity for r in snapshot.routes}) == 23


def test_duplicate_ambiguous_and_missing_references_are_rejected() -> None:
    providers = (ProviderMetadata("gateway-dev", "a"), ProviderMetadata("gateway-dev", "b"))
    models = (
        ModelMetadata(ModelIdentity("gateway-dev", "a", "same")),
        ModelMetadata(ModelIdentity("gateway-dev", "b", "same")),
    )
    with pytest.raises(ValueError, match="Duplicate or ambiguous"):
        CatalogueSnapshot(
            "gateway-dev", "rev-1", providers, models,
            (
                DiscoveredRoute(route("a", "same", "same-route")),
                DiscoveredRoute(route("b", "same", "same-route")),
            ),
        )
    with pytest.raises(ValueError, match="Duplicate or ambiguous"):
        CatalogueSnapshot("gateway-dev", "rev-1", providers, models + (models[0],))
    with pytest.raises(ValueError, match="unknown provider"):
        CatalogueSnapshot("gateway-dev", "rev-1", models=(ModelMetadata(route().model),))
    with pytest.raises(ValueError, match="unknown model"):
        CatalogueSnapshot(
            "gateway-dev", "rev-1",
            providers=(ProviderMetadata("gateway-dev", "new-provider"),),
            routes=(DiscoveredRoute(route()),),
        )
    with pytest.raises(ValueError, match="Cross-gateway"):
        CatalogueSnapshot(
            "gateway-dev", "rev-1",
            providers=(ProviderMetadata("other", "provider"),),
        )


def test_discovered_ready_is_not_approval_or_verified_destination() -> None:
    snapshot = CatalogueSnapshot(
        "gateway-dev", "rev-2",
        providers=(ProviderMetadata("gateway-dev", "new-provider"),),
        models=(ModelMetadata(route().model),),
        routes=(
            DiscoveredRoute(
                route(),
                availability=AvailabilityObservation(
                    AvailabilityState.READY, EvidenceSource.GATEWAY_REPORTED
                ),
            ),
        ),
    )
    assert snapshot.routes[0].availability.state == AvailabilityState.READY
    assert not hasattr(snapshot.routes[0], "approval")
    assert not hasattr(snapshot.routes[0], "inference_destination")
    assert TrustedRoutePolicy(route()).inference_destination == InferenceDestination.UNKNOWN
    assert TrustedRoutePolicy(route()).approval == ApprovalState.UNREVIEWED


def test_explicit_owner_review_is_required_for_approval_and_verified_local() -> None:
    with pytest.raises(ValueError, match="Approval requires"):
        TrustedRoutePolicy(route(), approval=ApprovalState.APPROVED)
    with pytest.raises(ValueError, match="Approval requires"):
        TrustedRoutePolicy(
            route(), approval=ApprovalState.APPROVED,
            inference_destination=InferenceDestination.CLOUD,
            evidence_source=EvidenceSource.GATEWAY_REPORTED,
            policy_revision="rev-1",
        )
    with pytest.raises(ValueError, match="Verified local"):
        TrustedRoutePolicy(
            route(), inference_destination=InferenceDestination.VERIFIED_LOCAL
        )
    assert approved().approval == ApprovalState.APPROVED


def test_credential_free_projection_and_unknown_capability_defaults() -> None:
    view = project_model_spec(model(), route(), approved(), "catalogue-r1")
    assert view.identity.route_id == "route-a"
    assert view.catalogue_revision == "catalogue-r1"
    assert view.inference_destination == InferenceDestination.VERIFIED_LOCAL
    assert view.capabilities.state("tools") == CapabilitySupport.UNKNOWN
    rendered = repr(view) + str(asdict(view))
    assert "DUMMY_SECRET_NEVER_COPY" not in rendered
    assert "18000" not in rendered
    assert not {"endpoint", "api_key", "headers"} & asdict(view).keys()


def test_projection_accepts_only_explicit_capability_evidence() -> None:
    evidence = CapabilityMetadata(
        claims=(
            CapabilityEvidence(
                "tools", CapabilitySupport.SUPPORTED, EvidenceSource.PROTOCOL_TESTED
            ),
        )
    )
    view = project_model_spec(model(), route(), approved(), "rev-1", evidence)
    assert view.capabilities.verified_supported("tools")
    assert view.capabilities.state("vision") == CapabilitySupport.UNKNOWN


def test_projection_rejects_route_and_destination_mismatch() -> None:
    with pytest.raises(ValueError, match="Route identity"):
        project_model_spec(model(), route("other"), approved(), "rev-1")
    with pytest.raises(ValueError, match="Route identity"):
        project_model_spec(model(), route(), approved(route(route_id="other")), "rev-1")
    with pytest.raises(ValueError, match="destination"):
        project_model_spec(model(), route(), TrustedRoutePolicy(route()), "rev-1")


def test_missing_pricing_and_availability_are_unknown() -> None:
    price = PricingProvenance()
    assert price.input_usd_per_million is None
    assert price.output_usd_per_million is None
    assert AvailabilityObservation().state == AvailabilityState.UNKNOWN
    with pytest.raises(ValueError, match="pricing"):
        PricingProvenance(Decimal("NaN"))
    with pytest.raises(ValueError, match="timestamp"):
        AvailabilityObservation(observed_at_epoch=-1)


def test_revision_and_immutable_collections_are_validated() -> None:
    assert CatalogueSnapshot("gateway-dev", "rev-1").revision != CatalogueSnapshot(
        "gateway-dev", "rev-2"
    ).revision
    with pytest.raises(ValueError, match="immutable tuples"):
        CatalogueSnapshot("gateway-dev", "rev-1", providers=[])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="catalogue revision"):
        CatalogueSnapshot("gateway-dev", "unsafe revision with spaces")
    with pytest.raises(ValueError, match="schema version"):
        CatalogueSnapshot("gateway-dev", "rev-1", schema_version=3)
