"""Advisory, task-aware ranking of already gateway-admitted metadata views.

The only supported entrypoint is Gateway.plan_task(), which first calls
Gateway.eligible() and rejects unqualified model filters. This module has no
adapter, secrets, model execution, dynamic provider calls or learning loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from max_core.gateway.catalogue import (
    ApprovalState,
    CapabilityMetadata,
    RouteIdentity,
    SelectionMetadata,
)
from max_core.gateway.contracts import GatewayRequest, InferenceDestination
from max_core.gateway.evaluation import OfflineEvaluationBook


class PlanningError(ValueError):
    """Fixed, sanitized advisory-planner error code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class TaskKind(StrEnum):
    CHAT = "chat"
    CODING = "coding"
    REASONING = "reasoning"
    RESEARCH = "research"
    TOOL_PLANNING = "tool_planning"
    STRUCTURED_EXTRACTION = "structured_extraction"
    VISION = "vision"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True)
class RankedRoute:
    identity: RouteIdentity
    catalogue_revision: str
    estimated_cost_usd: Decimal | None
    quality_score: Decimal | None
    p95_latency_ms: int | None
    priority: int


@dataclass(frozen=True, slots=True)
class TaskPlan:
    request_id: str
    task: TaskKind
    catalogue_revision: str
    candidates: tuple[RankedRoute, ...]
    reason: str | None = None


_TASK_CAPABILITIES: dict[TaskKind, tuple[str, ...]] = {
    TaskKind.CHAT: ("text",),
    TaskKind.CODING: ("text",),
    TaskKind.REASONING: ("text", "reasoning"),
    TaskKind.RESEARCH: ("text",),
    TaskKind.TOOL_PLANNING: ("text", "tools"),
    TaskKind.STRUCTURED_EXTRACTION: ("text", "structured_output"),
    TaskKind.VISION: ("text", "vision"),
    TaskKind.AUDIO: ("audio",),
}
# Existing adapters only process text and inert tool proposals. These task
# classes must stay non-executable until explicit protocol and output validation.
_UNSUPPORTED_TRANSPORTS = frozenset(
    {
        TaskKind.STRUCTURED_EXTRACTION,
        TaskKind.VISION,
        TaskKind.AUDIO,
    }
)


def _estimated_cost(view: SelectionMetadata, request: GatewayRequest) -> Decimal | None:
    price = view.pricing
    if price.input_usd_per_million is None or price.output_usd_per_million is None:
        return None
    return (
        price.input_usd_per_million * Decimal(request.max_input_tokens)
        + price.output_usd_per_million * Decimal(request.max_output_tokens)
    ) / Decimal(1_000_000)


def _rank_eligible_views(
    request: GatewayRequest,
    *,
    task: TaskKind,
    catalogue_revision: str,
    admitted: tuple[SelectionMetadata, ...],
    evaluations: OfflineEvaluationBook,
    exact_route: RouteIdentity | None = None,
) -> TaskPlan:
    """Package-private: Gateway.plan_task() owns mandatory admission.

    This function deliberately does NOT accept ModelSpec, adapter clients,
    authorization tokens or raw catalogue objects.
    """
    if not isinstance(task, TaskKind) or not isinstance(evaluations, OfflineEvaluationBook):
        raise PlanningError("invalid_task_or_evaluations")
    if exact_route is not None and not isinstance(exact_route, RouteIdentity):
        raise PlanningError("invalid_exact_route")
    if task in _UNSUPPORTED_TRANSPORTS:
        return TaskPlan(
            request.request_id, task, catalogue_revision, (), "transport_capability_unavailable"
        )
    if task == TaskKind.TOOL_PLANNING and "tools" not in request.required_capabilities:
        return TaskPlan(
            request.request_id, task, catalogue_revision, (), "tool_request_not_admitted"
        )
    ranked: list[RankedRoute] = []
    required = _TASK_CAPABILITIES[task]
    for view in admitted:
        if (
            view.approval != ApprovalState.APPROVED
            or view.inference_destination == InferenceDestination.UNKNOWN
            or view.catalogue_revision != catalogue_revision
        ):
            raise PlanningError("untrusted_candidate_metadata")
        if exact_route is not None and view.identity != exact_route:
            continue
        caps: CapabilityMetadata = view.capabilities
        if not all(caps.verified_supported(capability) for capability in required):
            continue
        observation = evaluations.lookup(
            view.identity,
            task_id=task.value,
            catalogue_revision=catalogue_revision,
        )
        ranked.append(
            RankedRoute(
                identity=view.identity,
                catalogue_revision=catalogue_revision,
                estimated_cost_usd=_estimated_cost(view, request),
                quality_score=observation.quality if observation is not None else None,
                p95_latency_ms=observation.p95_latency_ms if observation is not None else None,
                priority=view.priority,
            )
        )

    # Avoid fabricating quality for untested routes. Verified task-specific
    # evaluations come first; remaining candidates use stable owner priority.
    # Performance evidence never overrides admission/privacy/cost constraints.
    def ranking(route: RankedRoute) -> tuple[int, Decimal, int, int, str, str, str]:
        return (
            0 if route.quality_score is not None else 1,
            -route.quality_score if route.quality_score is not None else Decimal(0),
            route.p95_latency_ms if route.p95_latency_ms is not None else 3_600_001,
            route.priority,
            route.identity.model.provider_id,
            route.identity.model.model_id,
            route.identity.route_id,
        )

    ranked.sort(key=ranking)
    reason = (
        None
        if ranked
        else ("exact_route_not_eligible" if exact_route else "no_eligible_task_route")
    )
    return TaskPlan(request.request_id, task, catalogue_revision, tuple(ranked), reason)
