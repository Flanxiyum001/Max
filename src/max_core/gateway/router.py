"""Fail-closed model selection, privacy routing, and preflight USD ceilings."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from decimal import Decimal

from max_core.gateway.adapters import HTTPAdapter
from max_core.gateway.catalogue import (
    ApprovalState,
    CapabilityMetadata,
    RouteIdentity,
    SelectionMetadata,
    TrustedRoutePolicy,
    project_model_spec,
)
from max_core.gateway.contracts import (
    GatewayError,
    GatewayRequest,
    GatewayResponse,
    InferenceDestination,
    ModelSpec,
    Privacy,
    StreamEvent,
)
from max_core.gateway.evaluation import OfflineEvaluationBook
from max_core.gateway.normalize import actual_cost
from max_core.gateway.task_planner import (
    PlanningError,
    TaskKind,
    TaskPlan,
    _rank_eligible_views,
)


def _worst_case_cost(spec: ModelSpec, request: GatewayRequest) -> Decimal | None:
    if spec.input_usd_per_million is None or spec.output_usd_per_million is None:
        return None
    return (
        Decimal(request.max_input_tokens) * spec.input_usd_per_million
        + Decimal(request.max_output_tokens) * spec.output_usd_per_million
    ) / Decimal(1_000_000)


class Gateway:
    """No tools, model discovery, budget persistence or public endpoint live here."""

    def __init__(self, models: Sequence[ModelSpec], adapter: HTTPAdapter) -> None:
        keys = [(m.provider_id, m.model_id) for m in models]
        if len(set(keys)) != len(keys):
            raise ValueError("Duplicate provider-model configuration")
        self._models = tuple(models)
        self.adapter = adapter

    def selection_metadata(
        self,
        bindings: Sequence[tuple[RouteIdentity, TrustedRoutePolicy]],
        *,
        catalogue_revision: str,
        verified_capabilities: Mapping[RouteIdentity, CapabilityMetadata] | None = None,
    ) -> tuple[SelectionMetadata, ...]:
        """Return a secret-free model view from trusted route bindings.

        Only approved, classified routes appear in the returned candidate tuple.
        A result of () means no approved selection candidates, NOT an execution
        error. This does NOT authorize execution: only eligible() controls
        request admission and fallback, including cost/privacy checks.
        """
        if len(bindings) != len(self._models):
            raise ValueError("All configured models require explicit route bindings")
        configured = {(m.provider_id, m.model_id): m for m in self._models}
        seen: set[RouteIdentity] = set()
        route_slots: set[tuple[str, str]] = set()
        gateways: set[str] = set()
        views: list[SelectionMetadata] = []
        for identity, policy in bindings:
            slot = (identity.model.gateway_id, identity.route_id)
            if identity in seen or slot in route_slots:
                raise ValueError("Duplicate or ambiguous route binding")
            seen.add(identity)
            route_slots.add(slot)
            gateways.add(identity.model.gateway_id)
            spec = configured.get((identity.model.provider_id, identity.model.model_id))
            if spec is None:
                raise ValueError("Route binding has no matching configured model")
            if policy.identity != identity:
                raise ValueError("Trusted route identity does not match route binding")
            if policy.inference_destination != spec.inference_destination:
                raise ValueError("Trusted route destination does not match execution configuration")
            # Validate *all* bindings, including excluded ones, before selecting.
            # Unreviewed, disabled and unknown-destination routes never become candidates.
            if (
                policy.approval != ApprovalState.APPROVED
                or spec.inference_destination == InferenceDestination.UNKNOWN
            ):
                continue
            evidence = (
                verified_capabilities.get(identity) if verified_capabilities is not None else None
            )
            views.append(project_model_spec(spec, identity, policy, catalogue_revision, evidence))
        if len(gateways) > 1:
            raise ValueError("A selection view must belong to one gateway instance")
        if verified_capabilities is not None and set(verified_capabilities) - seen:
            raise ValueError("Capability evidence references an unknown route")
        return tuple(views)

    def plan_task(
        self,
        request: GatewayRequest,
        bindings: Sequence[tuple[RouteIdentity, TrustedRoutePolicy]],
        *,
        catalogue_revision: str,
        task: TaskKind,
        verified_capabilities: Mapping[RouteIdentity, CapabilityMetadata],
        evaluations: OfflineEvaluationBook | None = None,
        exact_route: RouteIdentity | None = None,
    ) -> TaskPlan:
        """Advisory only. Never executes or changes the execution route.

        Revalidate against the original trusted execution-time eligible() gate;
        only the resulting credential-free views reach the pure planner.
        """
        if not isinstance(request, GatewayRequest):
            raise PlanningError("invalid_gateway_request")
        if request.allowed_models:
            # Legacy unqualified model IDs are ambiguous across providers.
            raise PlanningError("unqualified_model_filter_forbidden")
        views = self.selection_metadata(
            bindings,
            catalogue_revision=catalogue_revision,
            verified_capabilities=verified_capabilities,
        )
        # Never transfer ModelSpec or its API key into task_planner.
        admissible = {
            (spec.provider_id, spec.model_id, spec.dialect)
            for spec in self.eligible(request)
        }
        safe_views = tuple(
            view
            for view in views
            if (
                view.identity.model.provider_id,
                view.identity.model.model_id,
                view.identity.dialect,
            ) in admissible
        )
        return _rank_eligible_views(
            request,
            task=task,
            catalogue_revision=catalogue_revision,
            admitted=safe_views,
            evaluations=evaluations if evaluations is not None else OfflineEvaluationBook(),
            exact_route=exact_route,
        )

    def eligible(self, request: GatewayRequest) -> tuple[ModelSpec, ...]:
        result: list[ModelSpec] = []
        for spec in self._models:
            if request.allowed_models and spec.model_id not in request.allowed_models:
                continue
            if request.allowed_providers and spec.provider_id not in request.allowed_providers:
                continue
            if request.allowed_regions and spec.region not in request.allowed_regions:
                continue
            if not request.required_capabilities <= spec.capabilities:
                continue
            if request.max_input_tokens + request.max_output_tokens > spec.context_tokens:
                continue
            # A loopback HTTP endpoint may forward prompts to cloud models.
            # UNKNOWN inference egress is always ineligible, regardless of host.
            if spec.inference_destination == InferenceDestination.UNKNOWN:
                continue
            if spec.inference_destination == InferenceDestination.CLOUD:
                if (
                    not request.cloud_allowed
                    or request.local_only
                    or request.privacy in {Privacy.CONFIDENTIAL, Privacy.RESTRICTED}
                    or request.max_cost_usd is None
                ):
                    continue
            cost = _worst_case_cost(spec, request)
            if spec.inference_destination == InferenceDestination.CLOUD:
                # Cloud always requires known pricing and a quoted request budget,
                # even when its proxy is on loopback.
                if cost is None or request.max_cost_usd is None or cost > request.max_cost_usd:
                    continue
            elif request.max_cost_usd is not None and (cost is None or cost > request.max_cost_usd):
                continue
            result.append(spec)
        return tuple(sorted(result, key=lambda x: (x.priority, x.provider_id, x.model_id)))

    def _candidates(self, request: GatewayRequest) -> tuple[ModelSpec, ...]:
        options = self.eligible(request)
        if not options:
            raise GatewayError("no_eligible_provider")
        return options

    @staticmethod
    def _postflight(result: GatewayResponse, request: GatewayRequest) -> GatewayResponse:
        if (
            request.max_cost_usd is not None
            and result.estimated_cost_usd is not None
            and result.estimated_cost_usd > request.max_cost_usd
        ):
            raise GatewayError("budget_exceeded")
        # Actual usage may be omitted by the provider. Never replace missing with zeros.
        return result

    async def complete(self, request: GatewayRequest) -> GatewayResponse:
        options = self._candidates(request)
        for index, spec in enumerate(options):
            # Repeat admission at every fallback boundary rather than trusting
            # an earlier candidate list for a different inference destination.
            if spec not in self.eligible(request):
                raise GatewayError("no_eligible_provider")
            try:
                result = await self.adapter.complete(spec, request)
                return self._postflight(result, request)
            except GatewayError as exc:
                # Only a pre-response, explicitly classified 429/503 may fail over.
                # Credential failures, uncertain timeouts, malformed responses and
                # privacy/budget denials must never be retried via another provider.
                if not (request.allow_fallback and exc.fallback_safe and index + 1 < len(options)):
                    raise
        raise GatewayError("no_eligible_provider")  # Defensive: loop always returns/raises.

    async def stream(self, request: GatewayRequest) -> AsyncIterator[StreamEvent]:
        # Streaming is deliberately sticky: never retry/fallback after any stream start,
        # including a partial provider response that could have incurred billing.
        spec = self._candidates(request)[0]
        async for event in self.adapter.stream(spec, request):
            if event.kind == "usage" and request.max_cost_usd is not None:
                actual = actual_cost(spec, event.usage)
                if actual is not None and actual > request.max_cost_usd:
                    raise GatewayError("budget_exceeded")
            yield event
