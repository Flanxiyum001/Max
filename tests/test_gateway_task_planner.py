"""Offline task ranking tests; no adapter request, token, or live provider call."""

from __future__ import annotations

from dataclasses import asdict, replace
from decimal import Decimal

import httpx
import pytest

from max_core.gateway.adapters import HTTPAdapter
from max_core.gateway.catalogue import (
    ApprovalState,
    CapabilityEvidence,
    CapabilityMetadata,
    CapabilitySupport,
    EvidenceSource,
    ModelIdentity,
    RouteIdentity,
    TrustedRoutePolicy,
)
from max_core.gateway.contracts import (
    ChatMessage,
    Dialect,
    GatewayRequest,
    InferenceDestination,
    ModelSpec,
    Privacy,
)
from max_core.gateway.evaluation import OfflineEvaluationBook, QualityObservation
from max_core.gateway.router import Gateway
from max_core.gateway.task_planner import PlanningError, TaskKind

REVISION = "catalogue-verified-1"


def spec(
    provider: str,
    *,
    model: str = "shared-model",
    destination: InferenceDestination = InferenceDestination.VERIFIED_LOCAL,
    priority: int = 50,
    price: Decimal | None = Decimal("1"),
    supports_tools: bool = False,
    context: int = 8192,
) -> ModelSpec:
    is_local = destination != InferenceDestination.CLOUD
    endpoint = (
        "http://127.0.0.1:18888/v1/chat/completions"
        if is_local
        else "https://example.ai/v1/chat/completions"
    )
    return ModelSpec(
        provider_id=provider,
        model_id=model,
        dialect=Dialect.OPENAI_CHAT,
        endpoint=endpoint,
        is_local=is_local,
        inference_destination=destination,
        context_tokens=context,
        capabilities=frozenset({"text", "tools"} if supports_tools else {"text"}),
        input_usd_per_million=price,
        output_usd_per_million=price,
        api_key="DUMMY_CREDENTIAL_SHOULD_NEVER_LEAK",
        priority=priority,
    )


def route(provider: str, *, model: str = "shared-model") -> RouteIdentity:
    return RouteIdentity(
        ModelIdentity("trusted-gateway", provider, model),
        f"route-{provider}",
        Dialect.OPENAI_CHAT,
    )


def binding(
    provider: str,
    *,
    destination: InferenceDestination = InferenceDestination.VERIFIED_LOCAL,
    approval: ApprovalState = ApprovalState.APPROVED,
) -> tuple[RouteIdentity, TrustedRoutePolicy]:
    identity = route(provider)
    return identity, TrustedRoutePolicy(
        identity=identity,
        approval=approval,
        inference_destination=destination,
        evidence_source=EvidenceSource.OWNER_REVIEWED,
        policy_revision="owner-approved-1",
    )


def evidence(
    *capabilities: str, source: EvidenceSource = EvidenceSource.PROTOCOL_TESTED
) -> CapabilityMetadata:
    return CapabilityMetadata(
        claims=tuple(
            CapabilityEvidence(name, CapabilitySupport.SUPPORTED, source)
            for name in sorted(capabilities)
        )
    )


def req(
    *,
    privacy: Privacy = Privacy.PUBLIC,
    cloud_allowed: bool = False,
    local_only: bool = False,
    max_cost_usd: Decimal | None = None,
    tools: bool = False,
    allowed_models: frozenset[str] = frozenset(),
    context: int = 512,
) -> GatewayRequest:
    return GatewayRequest(
        request_id="offline-task-123",
        messages=(ChatMessage("user", "test task"),),
        privacy=privacy,
        max_input_tokens=context,
        max_output_tokens=128,
        cloud_allowed=cloud_allowed,
        local_only=local_only,
        max_cost_usd=max_cost_usd,
        required_capabilities=frozenset({"text", "tools"} if tools else {"text"}),
        allowed_models=allowed_models,
    )


def gateway(*models: ModelSpec) -> Gateway:
    return Gateway(models, HTTPAdapter(httpx.AsyncClient()))


def test_admitted_local_chat_and_coding_are_ranked_without_secrets() -> None:
    g = gateway(spec("a", priority=30), spec("b", priority=10))
    bindings = (binding("a"), binding("b"))
    caps = {route("a"): evidence("text"), route("b"): evidence("text")}
    plan = g.plan_task(
        req(),
        bindings,
        catalogue_revision=REVISION,
        task=TaskKind.CODING,
        verified_capabilities=caps,
    )
    assert [c.identity.model.provider_id for c in plan.candidates] == ["b", "a"]
    assert plan.reason is None
    assert plan.task == TaskKind.CODING
    assert "DUMMY_CREDENTIAL_SHOULD_NEVER_LEAK" not in str(asdict(plan))
    assert "18888" not in str(asdict(plan))


def test_no_implicit_capabilities_from_legacy_model_spec() -> None:
    g = gateway(spec("a"))
    plan = g.plan_task(
        req(),
        (binding("a"),),
        catalogue_revision=REVISION,
        task=TaskKind.CHAT,
        verified_capabilities={},
    )
    assert plan.candidates == ()
    assert plan.reason == "no_eligible_task_route"


def test_unreviewed_and_disabled_are_never_ranked() -> None:
    g = gateway(spec("a"), spec("b"), spec("c"))
    bindings = (
        binding("a", approval=ApprovalState.UNREVIEWED),
        binding("b", approval=ApprovalState.DISABLED),
        binding("c"),
    )
    caps = {route(p): evidence("text") for p in ("a", "b", "c")}
    plan = g.plan_task(
        req(), bindings, catalogue_revision=REVISION, task=TaskKind.CHAT, verified_capabilities=caps
    )
    assert tuple(x.identity for x in plan.candidates) == (route("c"),)


def test_unknown_destination_rejected_through_central_admission() -> None:
    g = gateway(spec("a", destination=InferenceDestination.UNKNOWN))
    identity = route("a")
    policy = TrustedRoutePolicy(identity)
    plan = g.plan_task(
        req(cloud_allowed=True, max_cost_usd=Decimal("100")),
        ((identity, policy),),
        catalogue_revision=REVISION,
        task=TaskKind.CHAT,
        verified_capabilities={identity: evidence("text")},
    )
    assert plan.candidates == ()


def test_cloud_opt_in_cost_and_privacy_must_pass_gateway_eligible() -> None:
    g = gateway(spec("cloud", destination=InferenceDestination.CLOUD))
    pair = binding("cloud", destination=InferenceDestination.CLOUD)
    caps = {route("cloud"): evidence("text")}
    for request in (
        req(),
        req(cloud_allowed=True),
        req(cloud_allowed=True, max_cost_usd=Decimal("0")),
        req(cloud_allowed=True, max_cost_usd=Decimal("100"), local_only=True),
        req(privacy=Privacy.CONFIDENTIAL, cloud_allowed=True, max_cost_usd=Decimal("100")),
        req(privacy=Privacy.RESTRICTED, cloud_allowed=True, max_cost_usd=Decimal("100")),
    ):
        plan = g.plan_task(
            request,
            (pair,),
            catalogue_revision=REVISION,
            task=TaskKind.CHAT,
            verified_capabilities=caps,
        )
        assert plan.candidates == ()
    allowed = g.plan_task(
        req(cloud_allowed=True, max_cost_usd=Decimal("100")),
        (pair,),
        catalogue_revision=REVISION,
        task=TaskKind.CHAT,
        verified_capabilities=caps,
    )
    assert len(allowed.candidates) == 1
    assert allowed.candidates[0].estimated_cost_usd is not None


def test_cloud_unknown_price_is_not_admitted_despite_approved_metadata() -> None:
    g = gateway(spec("cloud", destination=InferenceDestination.CLOUD, price=None))
    plan = g.plan_task(
        req(cloud_allowed=True, max_cost_usd=Decimal("100")),
        (binding("cloud", destination=InferenceDestination.CLOUD),),
        catalogue_revision=REVISION,
        task=TaskKind.CHAT,
        verified_capabilities={route("cloud"): evidence("text")},
    )
    assert plan.candidates == ()


def test_exact_route_override_is_qualified_and_does_not_trigger_fallback() -> None:
    g = gateway(spec("a"), spec("b"))
    bindings = (binding("a"), binding("b"))
    caps = {route(p): evidence("text") for p in ("a", "b")}
    plan = g.plan_task(
        req(),
        bindings,
        catalogue_revision=REVISION,
        task=TaskKind.CHAT,
        verified_capabilities=caps,
        exact_route=route("b"),
    )
    assert tuple(p.identity for p in plan.candidates) == (route("b"),)
    missing = RouteIdentity(
        ModelIdentity("other-gateway", "b", "shared-model"),
        "route-b",
        Dialect.OPENAI_CHAT,
    )
    empty = g.plan_task(
        req(),
        bindings,
        catalogue_revision=REVISION,
        task=TaskKind.CHAT,
        verified_capabilities=caps,
        exact_route=missing,
    )
    assert empty.candidates == ()
    assert empty.reason == "exact_route_not_eligible"


def test_unqualified_model_filter_is_rejected_for_planner() -> None:
    g = gateway(spec("a"))
    with pytest.raises(PlanningError, match="unqualified_model_filter_forbidden"):
        g.plan_task(
            req(allowed_models=frozenset({"shared-model"})),
            (binding("a"),),
            catalogue_revision=REVISION,
            task=TaskKind.CHAT,
            verified_capabilities={route("a"): evidence("text")},
        )


def test_capability_evidence_must_be_verified_for_reasoning() -> None:
    g = gateway(spec("a"))
    weak = {route("a"): evidence("text", "reasoning", source=EvidenceSource.GATEWAY_REPORTED)}
    empty = g.plan_task(
        req(),
        (binding("a"),),
        catalogue_revision=REVISION,
        task=TaskKind.REASONING,
        verified_capabilities=weak,
    )
    assert empty.candidates == ()
    verified = {route("a"): evidence("text", "reasoning")}
    planned = g.plan_task(
        req(),
        (binding("a"),),
        catalogue_revision=REVISION,
        task=TaskKind.REASONING,
        verified_capabilities=verified,
    )
    assert len(planned.candidates) == 1


def test_tools_require_capability_evidence_and_request_admission() -> None:
    g = gateway(spec("a", supports_tools=True))
    caps = {route("a"): evidence("text", "tools")}
    no_tools = g.plan_task(
        req(),
        (binding("a"),),
        catalogue_revision=REVISION,
        task=TaskKind.TOOL_PLANNING,
        verified_capabilities=caps,
    )
    assert no_tools.candidates == ()
    assert no_tools.reason == "tool_request_not_admitted"
    yes = g.plan_task(
        req(tools=True),
        (binding("a"),),
        catalogue_revision=REVISION,
        task=TaskKind.TOOL_PLANNING,
        verified_capabilities=caps,
    )
    assert len(yes.candidates) == 1


@pytest.mark.parametrize(
    "task",
    [TaskKind.VISION, TaskKind.AUDIO, TaskKind.STRUCTURED_EXTRACTION],
)
def test_unsupported_transport_modes_remain_blocked(task: TaskKind) -> None:
    g = gateway(spec("a", supports_tools=True))
    caps = {route("a"): evidence("text", "vision", "audio", "structured_output", "tools")}
    plan = g.plan_task(
        req(), (binding("a"),), catalogue_revision=REVISION, task=task, verified_capabilities=caps
    )
    assert plan.candidates == ()
    assert plan.reason == "transport_capability_unavailable"


def test_context_overflow_remains_ineligible() -> None:
    g = gateway(spec("a", context=400))
    plan = g.plan_task(
        req(context=512),
        (binding("a"),),
        catalogue_revision=REVISION,
        task=TaskKind.CHAT,
        verified_capabilities={route("a"): evidence("text")},
    )
    assert plan.candidates == ()


def test_evaluated_models_use_reviewed_quality_then_latency() -> None:
    g = gateway(spec("a", priority=1), spec("b", priority=200))
    caps = {route(p): evidence("text") for p in ("a", "b")}
    book = OfflineEvaluationBook(
        observations=(
            QualityObservation(
                route("a"),
                "coding",
                REVISION,
                Decimal("0.6"),
                10,
                1200,
                EvidenceSource.OWNER_REVIEWED,
            ),
            QualityObservation(
                route("b"),
                "coding",
                REVISION,
                Decimal("0.9"),
                10,
                3000,
                EvidenceSource.PROTOCOL_TESTED,
            ),
        )
    )
    plan = g.plan_task(
        req(),
        (binding("a"), binding("b")),
        catalogue_revision=REVISION,
        task=TaskKind.CODING,
        verified_capabilities=caps,
        evaluations=book,
    )
    assert [x.identity.model.provider_id for x in plan.candidates] == ["b", "a"]
    assert plan.candidates[0].quality_score == Decimal("0.9")
    assert plan.candidates[1].p95_latency_ms == 1200


def test_stale_evaluation_data_never_changes_rank_order() -> None:
    g = gateway(spec("a", priority=1), spec("b", priority=100))
    caps = {route(p): evidence("text") for p in ("a", "b")}
    old = OfflineEvaluationBook(
        (
            QualityObservation(
                route("b"),
                "chat",
                "old-revision",
                Decimal("1"),
                100,
                50,
                EvidenceSource.OWNER_REVIEWED,
            ),
        )
    )
    result = g.plan_task(
        req(),
        (binding("a"), binding("b")),
        catalogue_revision=REVISION,
        task=TaskKind.CHAT,
        verified_capabilities=caps,
        evaluations=old,
    )
    assert [x.identity.model.provider_id for x in result.candidates] == ["a", "b"]
    assert all(x.quality_score is None for x in result.candidates)


def test_dialect_and_approval_binding_mismatch_still_rejected() -> None:
    g = gateway(spec("a"))
    original = binding("a")
    wrong = replace(original[1], inference_destination=InferenceDestination.CLOUD)
    with pytest.raises(ValueError, match="destination"):
        g.plan_task(
            req(),
            ((original[0], wrong),),
            catalogue_revision=REVISION,
            task=TaskKind.CHAT,
            verified_capabilities={route("a"): evidence("text")},
        )
