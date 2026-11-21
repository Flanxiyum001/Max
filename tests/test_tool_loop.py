from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from max_core.chat import request_factory
from max_core.gateway.contracts import (
    ChatMessage,
    GatewayRequest,
    GatewayResponse,
    InferenceDestination,
)
from max_core.gateway_trial import TrialSettings
from max_core.tool_loop import ToolLoop, parse_tool_calls
from max_core.tools import ToolError, Workspace, builtin_registry, safe_calculate


class ScriptedGateway:
    def __init__(self, replies: list[str]) -> None:
        self.replies = replies
        self.requests: list[GatewayRequest] = []

    async def complete(self, request: GatewayRequest) -> GatewayResponse:
        self.requests.append(request)
        content = self.replies.pop(0)
        return GatewayResponse(request.request_id, "p", "m", content, (), "stop", None, None)


def make(messages: tuple[ChatMessage, ...]) -> GatewayRequest:
    from max_core.gateway.contracts import Privacy

    size = sum(len(m.content.encode()) + 32 for m in messages)
    return GatewayRequest("r1", messages, Privacy.CONFIDENTIAL, size, 256, local_only=True)


def call(tool: str, **arguments: object) -> str:
    return f"<tool_call>{json.dumps({'name': tool, 'arguments': arguments})}</tool_call>"


def run(loop: ToolLoop, text: str = "hi"):  # type: ignore[no-untyped-def]
    return asyncio.run(loop.run([ChatMessage("user", text)], make))


def test_plain_answer_without_tools() -> None:
    gw = ScriptedGateway(["Hello."])
    result = run(ToolLoop(gw, builtin_registry()))
    assert result.answer == "Hello." and result.steps == []
    assert "Available tools" in gw.requests[0].messages[0].content


def test_read_only_tool_executes_and_result_is_fed_back() -> None:
    gw = ScriptedGateway([call("math.calculate", expression="6*7"), "It is 42."])
    result = run(ToolLoop(gw, builtin_registry()))
    assert result.answer == "It is 42."
    assert [s.status for s in result.steps] == ["proposed", "executed"]
    fed = gw.requests[1].messages[-1].content
    assert "untrusted data" in fed and '"output": "42"' in fed


def test_effectful_tool_denied_without_approver(tmp_path: Path) -> None:
    gw = ScriptedGateway([call("notes.write", name="a", content="x"), "ok"])
    result = run(ToolLoop(gw, builtin_registry(tmp_path)))
    assert result.steps[-1].status == "denied"
    assert not (tmp_path / "max-notes" / "a.md").exists()


def test_effectful_tool_runs_only_with_exact_approval(tmp_path: Path) -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def approve(name: str, args: dict[str, object]) -> bool:
        seen.append((name, args))
        return True

    gw = ScriptedGateway([call("notes.write", name="plan", content="# hi"), "saved"])
    result = run(ToolLoop(gw, builtin_registry(tmp_path), approve=approve))
    assert seen == [("notes.write", {"name": "plan", "content": "# hi"})]
    assert (tmp_path / "max-notes" / "plan.md").read_text() == "# hi"
    assert result.steps[-1].status == "executed"
    # never overwrites
    gw2 = ScriptedGateway([call("notes.write", name="plan", content="new"), "x"])
    r2 = run(ToolLoop(gw2, builtin_registry(tmp_path), approve=approve))
    assert r2.steps[-1].error_code == "note_exists"
    assert (tmp_path / "max-notes" / "plan.md").read_text() == "# hi"


def test_approver_exception_is_denial(tmp_path: Path) -> None:
    def boom(name: str, args: dict[str, object]) -> bool:
        raise RuntimeError

    gw = ScriptedGateway([call("notes.write", name="a", content="x"), "ok"])
    result = run(ToolLoop(gw, builtin_registry(tmp_path), approve=boom))
    assert result.steps[-1].status == "denied"


def test_unknown_malformed_and_bad_arguments() -> None:
    gw = ScriptedGateway(
        [
            call("shell.run", cmd="rm -rf /")
            + "<tool_call>{not json}</tool_call>"
            + call("math.calculate", expression=1, extra=2),
            "done",
        ]
    )
    result = run(ToolLoop(gw, builtin_registry()))
    codes = [s.error_code for s in result.steps]
    assert codes == ["unknown_tool", "unknown_tool", "unknown_argument"]


def test_step_limit_stops_loop() -> None:
    gw = ScriptedGateway([call("clock.now")] * 3)
    result = run(ToolLoop(gw, builtin_registry(), max_steps=3))
    assert result.stopped_reason == "step_limit" and len(gw.requests) == 3


def test_model_failure_is_sanitized() -> None:
    class Broken:
        async def complete(self, request: GatewayRequest) -> GatewayResponse:
            raise RuntimeError("secret provider detail")

    result = run(ToolLoop(Broken(), builtin_registry()))
    assert result.stopped_reason == "model_failed" and "secret" not in result.answer


def test_safe_calculate() -> None:
    assert safe_calculate("2**10 + sqrt(16)") == 1028.0
    for bad in ["__import__('os')", "(1).__class__", "9**9**9", "open('x')", "1/0"]:
        with pytest.raises(ToolError):
            safe_calculate(bad)


def test_workspace_confinement(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / ".env").write_text("KEY=1")
    (tmp_path / "secrets.txt").write_text("x")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("no")
    (tmp_path / "link").symlink_to(outside)
    ws = Workspace(tmp_path)
    assert ws.read({"path": "a.txt"}) == "hello"
    assert ws.list({}) == "a.txt\nlink"
    for bad in ["../outside.txt", "link", ".env", "secrets.txt", str(outside)]:
        with pytest.raises(ToolError):
            ws.read({"path": bad})


def test_parse_ignores_plain_text() -> None:
    resp = GatewayResponse("r", "p", "m", "no tools here", (), None, None, None)
    assert parse_tool_calls(resp) == []


def test_request_factory_local_vs_cloud() -> None:
    local = TrialSettings.from_env(
        {
            "MAX_TRIAL_DESTINATION": "verified_local",
            "MAX_TRIAL_LOCAL_EGRESS_REVIEWED": "yes",
            "MAX_TRIAL_ENDPOINT_LOCAL": "yes",
            "MAX_TRIAL_ENDPOINT": "http://127.0.0.1:11434/v1/chat/completions",
            "MAX_TRIAL_MODEL": "qwen2.5:7b",
            "MAX_TRIAL_CONTEXT_TOKENS": "8192",
        }
    )
    req = request_factory(local)((ChatMessage("user", "hi"),))
    assert req.local_only and not req.cloud_allowed
    assert local.spec.inference_destination == InferenceDestination.VERIFIED_LOCAL
