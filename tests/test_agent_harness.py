"""MAX harness cannot claim tool execution or leak model content through progress."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

import pytest

from max_core.agent_harness import ActivityKind, AgentHarness
from max_core.gateway.contracts import GatewayResponse, ToolProposal
from tests.test_gateway import request


def response(*, proposals: tuple[ToolProposal, ...] = ()) -> GatewayResponse:
    return GatewayResponse(
        request_id="fixture-001",
        provider_id="local",
        model_id="synthetic",
        content="SYNTHETIC_PRIVATE_MODEL_RESPONSE",
        tool_proposals=proposals,
        finish_reason="stop",
        usage=None,
        estimated_cost_usd=None,
    )


class FakeGateway:
    def __init__(self, reply: GatewayResponse | None = None, *, delay: float = 0) -> None:
        self.reply = reply or response()
        self.delay = delay
        self.calls = 0

    async def complete(self, _: Any) -> GatewayResponse:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.reply


def test_single_model_turn_records_real_progress_not_completion() -> None:
    gateway = FakeGateway()
    displayed: list[object] = []
    outcome = asyncio.run(AgentHarness(gateway).run(request(), on_activity=displayed.append))
    assert gateway.calls == 1
    assert outcome.state == "response_ready"
    assert not outcome.requires_owner_review
    assert [event.kind for event in outcome.events] == [
        ActivityKind.ACCEPTED,
        ActivityKind.MODEL_STARTED,
        ActivityKind.MODEL_RESPONDED,
        ActivityKind.RESPONSE_READY,
    ]
    assert list(outcome.events) == displayed
    assert "SYNTHETIC_PRIVATE" not in repr(outcome)
    assert not hasattr(outcome, "tool_executed")


def test_model_tool_calls_remain_inert_and_require_separate_owner_review() -> None:
    proposed = ToolProposal("call-1", "repo.read", {"path": "PRIVATE_PATH"})
    gateway = FakeGateway(response(proposals=(proposed,)))
    observed: list[object] = []
    outcome = asyncio.run(AgentHarness(gateway).run(request(), on_activity=observed.append))
    assert outcome.state == "review_required"
    assert outcome.requires_owner_review
    assert [event.kind for event in outcome.events][-2:] == [
        ActivityKind.TOOL_PROPOSED,
        ActivityKind.REVIEW_REQUIRED,
    ]
    assert outcome.events[-2].tool_name == "repo.read"
    assert "PRIVATE_PATH" not in repr(outcome.events)
    assert "PRIVATE_PATH" not in repr(observed)
    assert "PRIVATE_PATH" not in repr(outcome)
    # This is a proposal, not permission, tool execution or side-effect evidence.
    assert outcome.response is not None
    assert outcome.response.tool_proposals == (proposed,)


def test_malformed_or_duplicate_proposals_fail_closed() -> None:
    bad = [
        response(proposals=tuple(ToolProposal(f"c{n}", "safe_tool", {}) for n in range(9))),
        response(
            proposals=(ToolProposal("duplicate", "a", {}), ToolProposal("duplicate", "b", {}))
        ),
        response(proposals=(ToolProposal("c1", "unsafe/tool", {}),)),
        replace(response(), request_id="wrong-id"),
    ]
    for item in bad:
        outcome = asyncio.run(AgentHarness(FakeGateway(item)).run(request()))
        assert outcome.state == "failed"
        assert outcome.error_code == "invalid_model_proposals"
        assert outcome.response is None
        assert outcome.events[-1].kind == ActivityKind.FAILED


def test_no_raw_provider_failure_exposed_to_observer() -> None:
    class Broken:
        async def complete(self, _: Any) -> GatewayResponse:
            raise RuntimeError("SENSITIVE_EXCEPTION_CONTENT")

    outcome = asyncio.run(AgentHarness(Broken()).run(request()))
    assert outcome.state == "failed"
    assert outcome.error_code == "model_unavailable"
    assert "SENSITIVE_EXCEPTION_CONTENT" not in repr(outcome)


def test_timeout_is_bounded_and_does_not_complete() -> None:
    outcome = asyncio.run(
        AgentHarness(FakeGateway(delay=0.05), deadline_seconds=0.001).run(request())
    )
    assert outcome.state == "failed"
    assert outcome.error_code == "model_deadline_exceeded"


def test_cancelled_execution_propagates_cancellation_without_completion() -> None:
    observed: list[ActivityKind] = []

    async def run_and_cancel() -> None:
        async def wait() -> GatewayResponse:
            await asyncio.sleep(30)
            return response()

        class Slow:
            async def complete(self, _: Any) -> GatewayResponse:
                return await wait()

        async def run() -> None:
            await AgentHarness(Slow()).run(
                request(), on_activity=lambda event: observed.append(event.kind)
            )

        task = asyncio.create_task(run())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_and_cancel())
    assert observed[-1] == ActivityKind.CANCELLED
    assert ActivityKind.RESPONSE_READY not in observed


def test_invalid_configuration_and_callback_failure_are_bounded() -> None:
    with pytest.raises(ValueError, match="invalid_harness_deadline"):
        AgentHarness(FakeGateway(), deadline_seconds=0)
    with pytest.raises(ValueError, match="invalid_tool_proposal_budget"):
        AgentHarness(FakeGateway(), max_tool_proposals=9)

    def broken_display(_: object) -> None:
        raise RuntimeError("DISPLAY_ERROR_WITH_UNTRUSTED_DATA")

    outcome = asyncio.run(AgentHarness(FakeGateway()).run(request(), on_activity=broken_display))
    assert outcome.state == "response_ready"
