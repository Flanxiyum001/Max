"""Bounded MAX agent harness: genuine model activity, inert tool proposals.

This library does not authenticate owners, execute tools, manage device sessions,
store traces, approve work, or independently verify external effects. Its caller
must enforce MAX Trust and the gateway's privacy/egress restrictions.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol

from max_core.gateway.contracts import GatewayRequest, GatewayResponse, ToolProposal


class ActivityKind(StrEnum):
    ACCEPTED = "run.accepted"
    MODEL_STARTED = "model.started"
    MODEL_RESPONDED = "model.responded"
    TOOL_PROPOSED = "tool.proposed"
    REVIEW_REQUIRED = "review.required"
    RESPONSE_READY = "response.ready"
    FAILED = "run.failed"
    CANCELLED = "run.cancelled"


OutcomeState = Literal["response_ready", "review_required", "failed"]
_TOOL = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,99}")


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """Safe-to-display metadata, not a tool result or a Trust authorization."""

    sequence: int
    kind: ActivityKind
    tool_name: str | None = None

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or not 1 <= self.sequence <= 32:
            raise ValueError("invalid_activity_sequence")
        if not isinstance(self.kind, ActivityKind):
            raise ValueError("invalid_activity_kind")
        if self.kind == ActivityKind.TOOL_PROPOSED:
            if not isinstance(self.tool_name, str) or not _TOOL.fullmatch(self.tool_name):
                raise ValueError("invalid_activity_tool")
        elif self.tool_name is not None:
            raise ValueError("unexpected_activity_tool")


@dataclass(frozen=True, slots=True, repr=False)
class HarnessOutcome:
    """The normalized response is private; never expose it in event displays."""

    state: OutcomeState
    events: tuple[ActivityEvent, ...]
    response: GatewayResponse | None = field(default=None, repr=False)
    error_code: str | None = None

    @property
    def requires_owner_review(self) -> bool:
        return self.state == "review_required"


class CompletionGateway(Protocol):
    async def complete(self, request: GatewayRequest) -> GatewayResponse: ...


ActivitySink = Callable[[ActivityEvent], None]


class AgentHarness:
    """One bounded model turn; *no* autonomous tool loop or executors.

    A model may suggest tool names and arguments. They remain untrusted in the
    private response and are NEVER sent back as tool results or executed here.
    A live UI can subscribe to metadata-only progress using on_activity.
    """

    def __init__(
        self,
        gateway: CompletionGateway,
        *,
        deadline_seconds: float = 45.0,
        max_tool_proposals: int = 8,
    ) -> None:
        if not 1 <= deadline_seconds <= 120:
            raise ValueError("invalid_harness_deadline")
        if type(max_tool_proposals) is not int or not 1 <= max_tool_proposals <= 8:
            raise ValueError("invalid_tool_proposal_budget")
        self._gateway = gateway
        self._deadline_seconds = deadline_seconds
        self._max_tool_proposals = max_tool_proposals

    async def run(
        self,
        request: GatewayRequest,
        *,
        on_activity: ActivitySink | None = None,
    ) -> HarnessOutcome:
        """Owner-invoked, single completion, with sanitized real progress.

        A response_ready state means text was received, NOT that a coding task,
        external effect or tool operation was completed. No auto-retry/fallback
        beyond the existing gateway's explicitly configured policy.
        """
        if not isinstance(request, GatewayRequest):
            raise ValueError("invalid_harness_request")
        if on_activity is not None and not callable(on_activity):
            raise ValueError("invalid_activity_sink")

        events: list[ActivityEvent] = []

        def emit(kind: ActivityKind, tool_name: str | None = None) -> None:
            event = ActivityEvent(len(events) + 1, kind, tool_name)
            events.append(event)
            if on_activity is not None:
                try:
                    on_activity(event)
                except Exception:
                    # A local display failure cannot become a model/tool approval.
                    pass

        emit(ActivityKind.ACCEPTED)
        emit(ActivityKind.MODEL_STARTED)
        try:
            response = await asyncio.wait_for(
                self._gateway.complete(request), timeout=self._deadline_seconds
            )
        except asyncio.CancelledError:
            emit(ActivityKind.CANCELLED)
            raise
        except TimeoutError:
            emit(ActivityKind.FAILED)
            return HarnessOutcome("failed", tuple(events), error_code="model_deadline_exceeded")
        except Exception:
            # Suppress untrusted provider/transport exception messages and context.
            emit(ActivityKind.FAILED)
            return HarnessOutcome("failed", tuple(events), error_code="model_unavailable")

        if (
            not isinstance(response, GatewayResponse)
            or response.request_id != request.request_id
            or not isinstance(response.tool_proposals, tuple)
            or len(response.tool_proposals) > self._max_tool_proposals
            or any(
                not isinstance(tool, ToolProposal)
                or not isinstance(tool.name, str)
                or _TOOL.fullmatch(tool.name) is None
                for tool in response.tool_proposals
            )
            or len(
                {tool.call_id for tool in response.tool_proposals if isinstance(tool, ToolProposal)}
            )
            != len(response.tool_proposals)
        ):
            emit(ActivityKind.FAILED)
            return HarnessOutcome("failed", tuple(events), error_code="invalid_model_proposals")

        emit(ActivityKind.MODEL_RESPONDED)
        if response.tool_proposals:
            for proposal in response.tool_proposals:
                emit(ActivityKind.TOOL_PROPOSED, proposal.name)
            emit(ActivityKind.REVIEW_REQUIRED)
            return HarnessOutcome("review_required", tuple(events), response)
        emit(ActivityKind.RESPONSE_READY)
        return HarnessOutcome("response_ready", tuple(events), response)
