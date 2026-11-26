"""Tests for MAX Harness: lifecycle phases, Trust gating, retries, delegation, and verification."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from max_core.coding_tools import CodingWorkspace, register_coding_tools
from max_core.gateway.contracts import ChatMessage, GatewayRequest, GatewayResponse, Privacy
from max_core.harness import (
    ExecutionPlan,
    HarnessBudget,
    HarnessEvent,
    HarnessPhase,
    HarnessReport,
    MaxHarness,
    SubAgentSpec,
    VerificationResult,
    _compact_scratch,
)
from max_core.tools import Tool, ToolError, ToolParam, ToolRegistry, ToolRisk


class FakeGateway:
    def __init__(self, replies: Sequence[str]) -> None:
        self._replies = list(replies)
        self.requests: list[GatewayRequest] = []

    async def complete(self, request: GatewayRequest) -> GatewayResponse:
        self.requests.append(request)
        content = self._replies.pop(0) if self._replies else "done"
        return GatewayResponse(
            request_id=request.request_id,
            provider_id="local",
            model_id="test-model",
            content=content,
            tool_proposals=(),
            finish_reason="stop",
            usage=None,
            estimated_cost_usd=None,
        )


def _make_request(messages: tuple[ChatMessage, ...]) -> GatewayRequest:
    return GatewayRequest(
        request_id="req-1",
        messages=messages,
        privacy=Privacy.PUBLIC,
        max_input_tokens=16_000,
        max_output_tokens=512,
        local_only=True,
    )


def test_harness_lifecycle_phases_and_verification() -> None:
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="clock.now",
            description="Get current time",
            risk=ToolRisk.READ_ONLY,
            params=(),
            handler=lambda _args: "19:45",
        )
    )
    gw = FakeGateway(
        [
            '<tool_call>{"name": "clock.now", "arguments": {}}</tool_call>',
            "It is 19:45.",
        ]
    )
    events: list[HarnessEvent] = []
    harness = MaxHarness(
        gw,
        reg,
        on_event=events.append,
        verify=lambda report: VerificationResult(
            any(s.tool_name == "clock.now" and s.status == "executed" for s in report.steps),
            "clock_checked",
        ),
    )
    report = asyncio.run(harness.run([ChatMessage("user", "what time is it?")], _make_request))
    assert report.answer == "It is 19:45."
    assert report.verified is True
    assert report.verification_code == "clock_checked"
    assert report.stopped_reason == "answered"
    kinds = [e.kind for e in events]
    assert kinds == [
        "harness.started",
        "plan.ready",
        "model.started",
        "model.responded",
        "tool.proposed",
        "tool.executed",
        "model.started",
        "model.responded",
        "verify.passed",
        "harness.completed",
    ]
    assert events[0].phase == HarnessPhase.UNDERSTAND
    assert events[-1].phase == HarnessPhase.REPORT


def test_harness_retries_readonly_tool_once_on_transient_failure() -> None:
    calls = 0

    def flaky(_args: dict[str, Any]) -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ToolError("tool_failed")
        return "recovered-ok"

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="notes.read",
            description="Read note",
            risk=ToolRisk.READ_ONLY,
            params=(),
            handler=flaky,
        )
    )
    gw = FakeGateway(
        [
            '<tool_call>{"name": "notes.read", "arguments": {}}</tool_call>',
            "Read succeeded after retry.",
        ]
    )
    events: list[HarnessEvent] = []
    harness = MaxHarness(
        gw, reg, budget=HarnessBudget(max_retries_per_tool=1), on_event=events.append
    )
    report = asyncio.run(harness.run([ChatMessage("user", "read notes")], _make_request))
    assert calls == 2
    assert any(e.kind == "tool.retry" and e.phase == HarnessPhase.RECOVER for e in events)
    assert any(s.status == "executed" for s in report.steps)


def test_harness_never_retries_or_executes_denied_effectful_tool() -> None:
    ran = False

    def write_file(_args: dict[str, Any]) -> str:
        nonlocal ran
        ran = True
        return "written"

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="files.write",
            description="Write file",
            risk=ToolRisk.EFFECTFUL,
            params=(ToolParam("path", str, "Target path"),),
            handler=write_file,
        )
    )
    gw = FakeGateway(
        [
            '<tool_call>{"name": "files.write", "arguments": {"path": "a.txt"}}</tool_call>',
            "Okay, I did not write the file.",
        ]
    )
    harness = MaxHarness(gw, reg, approve=lambda _name, _args: False)
    report = asyncio.run(harness.run([ChatMessage("user", "write a.txt")], _make_request))
    assert ran is False
    assert any(s.status == "denied" and s.error_code == "owner_denied" for s in report.steps)


def test_harness_delegates_to_scoped_subagent_without_privilege_escalation() -> None:
    secret_ran = False

    def secret_action(_args: dict[str, Any]) -> str:
        nonlocal secret_ran
        secret_ran = True
        return "secret"

    reg = ToolRegistry()
    reg.register(
        Tool(
            name="clock.now",
            description="Get time",
            risk=ToolRisk.READ_ONLY,
            params=(),
            handler=lambda _args: "19:50",
        )
    )
    reg.register(
        Tool(
            name="vault.unlock",
            description="Sensitive",
            risk=ToolRisk.EFFECTFUL,
            params=(),
            handler=secret_action,
        )
    )

    # Sub-agent tries to call vault.unlock even though it was only granted clock.now
    gw = FakeGateway(
        [
            '<tool_call>{"name": "vault.unlock", "arguments": {}}</tool_call>',
            '<tool_call>{"name": "clock.now", "arguments": {}}</tool_call>',
            "Time is 19:50.",
        ]
    )
    parent_report = HarnessReport(answer="")
    harness = MaxHarness(gw, reg, approve=lambda _n, _a: True)
    child_report = asyncio.run(
        harness.delegate(
            SubAgentSpec(role="timekeeper", allowed_tools=frozenset({"clock.now"})),
            "Check the time",
            _make_request,
            parent_report=parent_report,
        )
    )
    assert secret_ran is False
    assert child_report.answer == "Time is 19:50."
    assert parent_report.subagent_runs == 1
    assert any(
        s.tool_name == "vault.unlock" and s.error_code == "unknown_tool" for s in child_report.steps
    )
    assert any(s.tool_name == "clock.now" and s.status == "executed" for s in child_report.steps)


def test_harness_verification_failure_marks_report_unverified() -> None:
    reg = ToolRegistry()
    gw = FakeGateway(["I definitely ran the tests."])
    harness = MaxHarness(
        gw,
        reg,
        verify=lambda report: VerificationResult(
            any(s.status == "executed" for s in report.steps),
            "no_tool_evidence",
        ),
    )
    report = asyncio.run(harness.run([ChatMessage("user", "run tests")], _make_request))
    assert report.verified is False
    assert report.verification_code == "no_tool_evidence"
    assert report.stopped_reason == "verification_failed"


def test_harness_enforces_subagent_depth_limit() -> None:
    reg = ToolRegistry()
    gw = FakeGateway(["ok"])
    harness = MaxHarness(gw, reg, budget=HarnessBudget(max_subagent_depth=0))
    with pytest.raises(ValueError, match="subagent_depth_exhausted"):
        asyncio.run(
            harness.delegate(
                SubAgentSpec(role="helper"),
                "do something",
                _make_request,
            )
        )


def test_harness_execution_plan_updates_and_reports_steps() -> None:
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="clock.now",
            description="Get time",
            risk=ToolRisk.READ_ONLY,
            params=(),
            handler=lambda _args: "20:00",
        )
    )
    gw = FakeGateway(
        [
            '<tool_call>{"name": "plan.update", "arguments": {"steps_json": "[{\\"id\\": \\"1\\", \\"title\\": \\"Check clock\\", \\"status\\": \\"in_progress\\"}]"}}</tool_call>',
            '<tool_call>{"name": "clock.now", "arguments": {}}</tool_call>',
            '<tool_call>{"name": "plan.update", "arguments": {"steps_json": "[{\\"id\\": \\"1\\", \\"title\\": \\"Check clock\\", \\"status\\": \\"completed\\", \\"note\\": \\"20:00\\"}]"}}</tool_call>',
            "Done: 20:00.",
        ]
    )
    events: list[HarnessEvent] = []
    harness = MaxHarness(gw, reg, on_event=events.append)
    report = asyncio.run(harness.run([ChatMessage("user", "check time")], _make_request))
    assert report.answer == "Done: 20:00."
    assert len(report.plan) == 1
    assert report.plan[0].status == "completed"
    assert report.plan[0].note == "20:00"
    assert any(e.kind == "plan.updated" and e.phase == HarnessPhase.PLAN for e in events)


def test_harness_detects_repeated_failing_tool_loop() -> None:
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="notes.read",
            description="Read",
            risk=ToolRisk.READ_ONLY,
            params=(ToolParam("path", str, "Path"),),
            handler=lambda _a: (_ for _ in ()).throw(ToolError("not_a_file")),
        )
    )
    gw = FakeGateway(
        [
            '<tool_call>{"name": "notes.read", "arguments": {"path": "missing.txt"}}</tool_call>',
            '<tool_call>{"name": "notes.read", "arguments": {"path": "missing.txt"}}</tool_call>',
            "Could not find missing.txt.",
        ]
    )
    events: list[HarnessEvent] = []
    harness = MaxHarness(gw, reg, on_event=events.append)
    report = asyncio.run(harness.run([ChatMessage("user", "read missing.txt")], _make_request))
    assert report.answer == "Could not find missing.txt."
    assert any(e.kind == "loop.detected" and e.phase == HarnessPhase.RECOVER for e in events)
    assert "Repeated failing tool call detected" in gw.requests[-1].messages[-1].content


def test_harness_stops_exact_failure_replay_and_sanitizes_diagnosis() -> None:
    executions = 0
    secret = "private-path-user-token"

    def broken(_args: object) -> str:
        nonlocal executions
        executions += 1
        raise RuntimeError(secret)

    reg = ToolRegistry((Tool("test.read", "Read", ToolRisk.READ_ONLY, (), broken),))
    proposal = '<tool_call>{"name": "test.read", "arguments": {}}</tool_call>'
    gw = FakeGateway([proposal] * 5)
    report = asyncio.run(MaxHarness(gw, reg).run([ChatMessage("user", "read")], _make_request))
    assert report.error_code == "repeated_tool_failure"
    # The harness may retry read-only calls once within each of two distinct proposals.
    assert executions == 4
    assert len(gw.requests) == 3
    assert all(secret not in str(request.messages) for request in gw.requests)


def test_harness_can_research_public_error_then_report_blocker() -> None:
    reg = ToolRegistry(
        (
            Tool(
                "test.read",
                "Read",
                ToolRisk.READ_ONLY,
                (),
                lambda _: (_ for _ in ()).throw(ToolError("network_error")),
            ),
            Tool(
                "web.search",
                "Public search",
                ToolRisk.READ_ONLY,
                (ToolParam("query", str, "Search"),),
                lambda args: "Official service docs for " + args["query"],
            ),
        )
    )
    gw = FakeGateway(
        [
            '<tool_call>{"name": "test.read", "arguments": {}}</tool_call>',
            '<tool_call>{"name": "web.search", "arguments": {"query": "network_error tool service documentation"}}</tool_call>',
            "The service is unavailable; check its status before trying again.",
        ]
    )
    report = asyncio.run(MaxHarness(gw, reg).run([ChatMessage("user", "read")], _make_request))
    assert report.stopped_reason == "answered"
    assert [step.tool_name for step in report.steps if step.status == "executed"] == ["web.search"]
    assert "generic error terms" in gw.requests[1].messages[-1].content


def test_compact_scratch_preserves_plan_and_recent_turns() -> None:
    plan = ExecutionPlan()
    plan.update('[{"id": "1", "title": "Fix bug", "status": "in_progress"}]')
    scratch = [ChatMessage("user" if i % 2 else "assistant", f"msg-{i}") for i in range(20)]
    compacted = _compact_scratch(scratch, plan)
    assert len(compacted) == 8
    assert "[Context summary of earlier steps]" in compacted[0].content
    assert "Fix bug" in compacted[0].content
    assert compacted[-1].content == "msg-19"


def test_coding_tools_read_search_edit_write_and_block_secrets(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def greet():\n    return 'hi'\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=123\n", encoding="utf-8")

    cw = CodingWorkspace(tmp_path)
    assert "1: def greet():" in cw.read_lines({"path": "app.py", "start_line": 1, "end_line": 2})
    assert "app.py:2:     return 'hi'" in cw.search({"pattern": "return 'hi'"})

    with pytest.raises(ToolError, match="path_not_allowed"):
        cw.read_lines({"path": ".env"})

    assert (
        cw.edit({"path": "app.py", "target": "'hi'", "replacement": "'hello'"}) == "edited app.py"
    )
    assert "'hello'" in (tmp_path / "app.py").read_text(encoding="utf-8")

    with pytest.raises(ToolError, match="target_not_found"):
        cw.edit({"path": "app.py", "target": "nonexistent", "replacement": "x"})

    assert cw.write({"path": "notes.txt", "content": "ok"}) == "wrote notes.txt (2 chars)"
    with pytest.raises(ToolError, match="file_exists"):
        cw.write({"path": "notes.txt", "content": "no"})

    reg = register_coding_tools(ToolRegistry(), tmp_path)
    assert reg.get("code.read") is not None
    assert reg.get("code.edit") is not None
    assert reg.get("code.run_tests") is not None


def test_harness_research_plan_self_prompt_and_guardrail(tmp_path: Path) -> None:
    (tmp_path / "svc.py").write_text("MODE = 'draft'\n", encoding="utf-8")
    reg = register_coding_tools(ToolRegistry(), tmp_path)
    reg.register(
        Tool(
            "web.search",
            "Search the web",
            ToolRisk.READ_ONLY,
            (ToolParam("query", str, "Query"),),
            lambda _a: "Best practice: use production strict mode.",
        )
    )

    gw = FakeGateway(
        [
            # 1. Try editing before planning/self-prompting -> blocked by guardrail
            '<tool_call>{"name": "code.edit", "arguments": {"path": "svc.py", "target": "draft", "replacement": "prod"}}</tool_call>',
            # 2. Research on the web
            '<tool_call>{"name": "web.search", "arguments": {"query": "best production mode"}}</tool_call>',
            # 3. Create complete plan
            '<tool_call>{"name": "plan.update", "arguments": {"steps_json": "[{\\"id\\": \\"1\\", \\"title\\": \\"Update MODE to prod\\", \\"status\\": \\"in_progress\\"}]"}}</tool_call>',
            # 4. Prompt himself
            '<tool_call>{"name": "prompt.set", "arguments": {"chosen_option": "Strict prod mode", "rationale": "Web research confirmed strict prod is safest", "self_prompt": "Edit svc.py cleanly and verify before reporting."}}</tool_call>',
            # 5. Now edit succeeds under his self-prompt
            '<tool_call>{"name": "code.edit", "arguments": {"path": "svc.py", "target": "draft", "replacement": "prod"}}</tool_call>',
            "Updated svc.py to prod mode.",
        ]
    )
    events: list[HarnessEvent] = []
    harness = MaxHarness(
        gw,
        reg,
        approve=lambda _n, _a: True,
        on_event=events.append,
        require_plan_and_self_prompt=True,
    )
    report = asyncio.run(harness.run([ChatMessage("user", "upgrade svc.py")], _make_request))
    assert report.answer == "Updated svc.py to prod mode."
    assert "Strict prod mode" in report.research_summary
    assert "Edit svc.py cleanly" in report.self_prompt
    assert (tmp_path / "svc.py").read_text(encoding="utf-8") == "MODE = 'prod'\n"
    assert any(
        s.tool_name == "code.edit" and s.error_code == "plan_and_self_prompt_required"
        for s in report.steps
    )
    assert any(e.kind == "web.researched" and e.phase == HarnessPhase.RESEARCH for e in events)
    assert any(
        e.kind == "self_prompt.ready" and e.phase == HarnessPhase.SELF_PROMPT for e in events
    )
    # Self-prompt was injected into the model's system messages for step 5+
    assert any(
        "[Self-Instruction Prompt]" in m.content
        for m in gw.requests[-1].messages
        if m.role == "system"
    )
