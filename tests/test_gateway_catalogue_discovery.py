"""Pure offline tests for FreeLLMAPI-shaped catalogue import and safe revisioning."""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

import pytest

from max_core.gateway.catalogue import (
    ApprovalState,
    AvailabilityState,
    CapabilitySupport,
    EvidenceSource,
    TrustedRoutePolicy,
)
from max_core.gateway.catalogue_discovery import (
    CatalogueImportError,
    parse_freellmapi_models,
)
from max_core.gateway.contracts import Dialect, InferenceDestination


def entry(
    model_id: str, provider: str = "provider-x", **extra: object
) -> dict[str, object]:
    return {
        "id": model_id,
        "object": "model",
        "owned_by": provider,
        "context_window": 8192,
        "context_length": 8192,
        "available": True,
        "execution_status": "ready",
        **extra,
    }


def payload(*rows: dict[str, object]) -> dict[str, object]:
    return {"object": "list", "data": list(rows)}


def test_arbitrary_providers_and_model_names_no_hardcoded_provider_list() -> None:
    rows = [entry(f"org/model-{i}", f"provider-{i}") for i in range(40)]
    imported = parse_freellmapi_models(payload(*rows), gateway_id="gw-A")
    assert len(imported.snapshot.providers) == 40
    assert len(imported.snapshot.models) == 40
    assert len(imported.snapshot.routes) == 40
    assert all(len(r.identity.route_id) == 66 for r in imported.snapshot.routes)
    assert imported.snapshot.revision.startswith("v1_")
    assert all(r.pricing.input_usd_per_million is None for r in imported.snapshot.routes)
    assert all(r.pricing.output_usd_per_million is None for r in imported.snapshot.routes)


def test_concrete_only_skips_proxy_virtuals_and_claude_aliases() -> None:
    rows = [
        entry("auto", "freellmapi"),
        entry("fusion", "freellmapi"),
        entry("auto:coding", "freellmapi"),
        entry("claude-sonnet-4-5", "freellmapi"),
        entry("actual-vision-model", "provider-x"),
    ]
    result = parse_freellmapi_models(payload(*rows), gateway_id="gw-A")
    assert len(result.snapshot.routes) == 1
    assert result.ignored_virtual_entries == 4
    assert result.snapshot.routes[0].identity.model.model_id == "actual-vision-model"


def test_aggregated_proxy_owner_remains_unapproved_unverified() -> None:
    result = parse_freellmapi_models(
        payload(entry("shared/group-model", "freellmapi")), gateway_id="gw-A"
    )
    route = result.snapshot.routes[0]
    assert route.identity.model.provider_id == "freellmapi"
    assert not hasattr(route, "approval")
    assert not hasattr(route, "inference_destination")
    policy = TrustedRoutePolicy(route.identity)
    assert policy.approval == ApprovalState.UNREVIEWED
    assert policy.inference_destination == InferenceDestination.UNKNOWN


def test_ready_means_only_reported_readiness_not_health_or_approval() -> None:
    result = parse_freellmapi_models(
        payload(entry("model-a")), gateway_id="gw-A", observed_at_epoch=123
    )
    observation = result.snapshot.routes[0].availability
    assert observation.state == AvailabilityState.READY
    assert observation.source == EvidenceSource.GATEWAY_REPORTED
    assert observation.observed_at_epoch == 123
    assert result.snapshot.models[0].capabilities.state("text") == CapabilitySupport.UNKNOWN


def test_unknown_status_and_contradictions_fail_closed_to_unknown() -> None:
    for status, available in [
        ("ready", False),
        ("exhausted", True),
        ("needsKey", True),
        ("unknown-future-status", True),
    ]:
        result = parse_freellmapi_models(
            payload(entry("model-a", execution_status=status, available=available)),
            gateway_id="gw-A",
        )
        assert result.snapshot.routes[0].availability.state == AvailabilityState.UNKNOWN


@pytest.mark.parametrize(
    ("status", "expected"),
    [("needsKey", AvailabilityState.NEEDS_KEY), ("exhausted", AvailabilityState.EXHAUSTED)],
)
def test_explicit_unavailability(status: str, expected: AvailabilityState) -> None:
    result = parse_freellmapi_models(
        payload(entry("model-a", execution_status=status, available=False)), gateway_id="gw-A"
    )
    assert result.snapshot.routes[0].availability.state == expected


def test_supported_parameters_are_unverified_claims_not_capability_grants() -> None:
    result = parse_freellmapi_models(
        payload(entry("model-a", supported_parameters=["tools", "response_format"])),
        gateway_id="gw-A",
    )
    capabilities = result.snapshot.models[0].capabilities
    assert capabilities.state("tools") == CapabilitySupport.SUPPORTED
    assert capabilities.state("structured_output") == CapabilitySupport.SUPPORTED
    assert capabilities.state("vision") == CapabilitySupport.UNKNOWN
    assert not capabilities.verified_supported("tools")
    assert not capabilities.verified_supported("structured_output")
    assert all(c.source == EvidenceSource.GATEWAY_REPORTED for c in capabilities.claims)


def test_model_name_does_not_imply_vision_reasoning_or_tools() -> None:
    result = parse_freellmapi_models(
        payload(entry("super-vision-reasoning-tools-model")), gateway_id="gw-A"
    )
    claims = result.snapshot.models[0].capabilities
    assert claims.state("vision") == CapabilitySupport.UNKNOWN
    assert claims.state("reasoning") == CapabilitySupport.UNKNOWN
    assert claims.state("tools") == CapabilitySupport.UNKNOWN


def test_different_list_order_same_content_revision_and_route_ids() -> None:
    a = entry("a", "owner-a")
    b = entry("b", "owner-b")
    forward = parse_freellmapi_models(payload(a, b), gateway_id="gw-A")
    reverse = parse_freellmapi_models(payload(b, a), gateway_id="gw-A")
    assert forward.snapshot == reverse.snapshot
    assert forward.snapshot.revision == reverse.snapshot.revision


def test_route_identity_stable_when_readiness_changes() -> None:
    ready = parse_freellmapi_models(payload(entry("a")), gateway_id="gw-A")
    exhausted = parse_freellmapi_models(
        payload(entry("a", available=False, execution_status="exhausted")), gateway_id="gw-A"
    )
    assert ready.snapshot.routes[0].identity == exhausted.snapshot.routes[0].identity
    assert ready.snapshot.revision != exhausted.snapshot.revision


def test_distinct_gateway_dialect_provider_and_model_produce_new_route() -> None:
    data = payload(entry("a", "vendor-a"))
    base = parse_freellmapi_models(data, gateway_id="gw-A")
    different_gateway = parse_freellmapi_models(data, gateway_id="gw-B")
    different_dialect = parse_freellmapi_models(
        data, gateway_id="gw-A", dialect=Dialect.ANTHROPIC_MESSAGES
    )
    different_provider = parse_freellmapi_models(
        payload(entry("a", "vendor-b")), gateway_id="gw-A"
    )
    different_model = parse_freellmapi_models(
        payload(entry("b", "vendor-a")), gateway_id="gw-A"
    )
    ids = {
        x.snapshot.routes[0].identity.route_id
        for x in (base, different_gateway, different_dialect, different_provider, different_model)
    }
    assert len(ids) == 5


def test_name_collision_across_providers_is_explicitly_reported() -> None:
    result = parse_freellmapi_models(
        payload(entry("same-model", "provider-a"), entry("same-model", "provider-b")),
        gateway_id="gw-A",
    )
    assert result.ambiguous_model_ids == ("same-model",)
    assert len(result.snapshot.routes) == 2
    assert (
        result.snapshot.routes[0].identity.route_id
        != result.snapshot.routes[1].identity.route_id
    )


@pytest.mark.parametrize(
    "broken",
    [
        None,
        {},
        [],
        {"object": "list"},
        {"object": "model", "data": []},
        {"object": "list", "data": "not-list"},
        {"object": "list", "data": [None]},
        payload({"id": "x", "owned_by": "provider-a"}),
        payload(entry("invalid model")),
        payload(entry("sk-EXAMPLE_very_long_secret")),
        payload(entry("x", "provider with spaces")),
        payload(entry("x", context_window=0)),
        payload(entry("x", context_window=True)),
        payload(entry("x", context_window=123, context_length=124)),
        payload(entry("x", available="true")),
        payload(entry("x", execution_status=123)),
        payload(entry("x", supported_parameters=["tools", "tools"])),
        payload(entry("x", supported_parameters=["this has spaces"])),
        payload(entry("x", supported_parameters="tools")),
        payload(entry("x"), entry("x")),
        payload(entry("x", context_window=Decimal("1024"))),
    ],
)
def test_malformed_catalogue_fails_closed_with_safe_error(broken: object) -> None:
    with pytest.raises(CatalogueImportError) as error:
        parse_freellmapi_models(broken, gateway_id="gw-A")
    assert error.value.code in {
        "invalid_catalogue_payload",
        "conflicting_catalogue_context",
        "duplicate_catalogue_parameter",
        "duplicate_catalogue_model",
    }


def test_unknown_raw_fields_cannot_become_secrets_or_policy_fields() -> None:
    marker = "UNTRUSTED_SECRET_VALUE_MUST_NOT_SURVIVE"
    raw = entry(
        "model-a",
        endpoint="https://sensitive.invalid/path",
        api_key=marker,
        authorization=marker,
        inference_destination="verified_local",
        approval="approved",
        input_usd_per_million="0",
        extra={"headers": {"authorization": marker}},
    )
    imported = parse_freellmapi_models(payload(raw), gateway_id="gw-A")
    assert marker not in repr(imported)
    assert marker not in str(asdict(imported))
    assert "sensitive.invalid" not in str(asdict(imported))
    assert imported.snapshot.routes[0].pricing.input_usd_per_million is None
    assert not hasattr(imported.snapshot.routes[0], "endpoint")


def test_bounded_row_count_and_payload_size() -> None:
    too_many = payload(*[entry(f"m-{i}") for i in range(4097)])
    with pytest.raises(CatalogueImportError, match="too_many_catalogue_entries"):
        parse_freellmapi_models(too_many, gateway_id="gw-A")
    huge = payload(entry("model-a", ignored="X" * 2_000_100))
    with pytest.raises(CatalogueImportError, match="catalogue_too_large"):
        parse_freellmapi_models(huge, gateway_id="gw-A")


def test_invalid_dialect_gateway_id_and_observation_time_are_rejected() -> None:
    data = payload(entry("model-a"))
    with pytest.raises(CatalogueImportError, match="invalid_gateway_id"):
        parse_freellmapi_models(data, gateway_id="invalid gateway")
    with pytest.raises(CatalogueImportError, match="invalid_catalogue_dialect"):
        parse_freellmapi_models(
            data, gateway_id="gw-A", dialect="openai_chat"  # type: ignore[arg-type]
        )
    with pytest.raises(CatalogueImportError, match="invalid_observation_timestamp"):
        parse_freellmapi_models(data, gateway_id="gw-A", observed_at_epoch=-1)


def test_cyclic_or_non_json_python_input_has_sanitized_error() -> None:
    cyclic: dict[str, object] = {"object": "list", "data": []}
    cyclic["self"] = cyclic
    with pytest.raises(CatalogueImportError, match="invalid_catalogue_payload"):
        parse_freellmapi_models(cyclic, gateway_id="gw-A")
    with pytest.raises(CatalogueImportError, match="invalid_catalogue_payload"):
        parse_freellmapi_models(payload(entry("x", field=object())), gateway_id="gw-A")

def test_available_false_without_status_is_reported_unavailable() -> None:
    result = parse_freellmapi_models(
        payload(entry("model-a", available=False, execution_status=None)), gateway_id="gw-A"
    )
    assert result.snapshot.routes[0].availability.state == AvailabilityState.UNAVAILABLE

