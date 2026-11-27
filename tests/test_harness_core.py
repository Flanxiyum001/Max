from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from max_core.gateway.contracts import ChatMessage, GatewayRequest, GatewayResponse, Privacy
from max_core.harness_core import (
    AgentLoop,
    Context,
    Plugin,
    RecordingGateway,
    ReplayGateway,
    SessionLog,
    derive_messages,
)
from max_core.tools import Tool, ToolParam, ToolRegistry, ToolRisk, builtin_registry

SNAPSHOT = Path(__file__).parent / "snapshots" / "harness_core_clock.json"


def make(messages: tuple[ChatMessage, ...]) -> GatewayRequest:
    size = sum(len(m.content.encode()) + 32 for m in messages)
    return GatewayRequest("r1", messages, Privacy.CONFIDENTIAL, size, 256, local_only=True)


class Scripted:
    def __init__(self, replies: list[str]) -> None:
        self.replies, self.requests = replies, []

    async def complete(self, request: GatewayRequest) -> GatewayResponse:
        self.requests.append(request)
        return GatewayResponse(
            request.request_id, "p", "m", self.replies.pop(0), (), "stop", None, None
        )


def call(name: str, **args: object) -> str:
    return f"<tool_call>{json.dumps({'name': name, 'arguments': args})}</tool_call>"


def writer(log: list[str]) -> ToolRegistry:
    def handler(args):  # type: ignore[no-untyped-def]
        log.append(args["text"])
        return "ok"

    return ToolRegistry(
        (Tool("notes.write", "w", ToolRisk.EFFECTFUL, (ToolParam("text", str, "t"),), handler),)
    )


def test_model_visible_means_logged() -> None:
    gw = Scripted([call("math.calculate", expression="2+2"), "4"])
    loop = AgentLoop(Context(), gw, builtin_registry())
    log = loop.start_session("be brief")
    result = asyncio.run(loop.run_turn(log, "2+2?", make))
    assert result.answer == "4" and result.stopped_reason == "answered" and result.steps == 2
    # every request equals the log projection at the time it was made
    assert gw.requests[-1].messages == derive_messages(log)[:-1]
    kinds = [e.kind for e in log]
    assert kinds[2:4] == ["turn/start", "user/message"] and kinds[-1] == "turn/end"


def test_session_round_trip(tmp_path: Path) -> None:
    log = SessionLog()
    log.append("user/message", text="hi")
    path = log.save(tmp_path)
    assert path.name == "session.v1.jsonl"
    assert derive_messages(SessionLog.load(path)) == derive_messages(log)


def test_log_rejects_unknown_and_gaps() -> None:
    with pytest.raises(ValueError):
        SessionLog().append("tool/approved")
    from max_core.harness_core import SessionEvent

    with pytest.raises(ValueError):
        SessionLog([SessionEvent(1, "user/message", {})])


def test_effectful_needs_owner_and_hooks_cannot_approve() -> None:
    written: list[str] = []
    ctx = Context()
    ctx.load(Plugin("yes-man", lambda c: c.on("tools/pre-execute", lambda p: True)))
    gw = Scripted([call("notes.write", text="x"), "done"])
    loop = AgentLoop(ctx, gw, writer(written))
    log = loop.start_session()
    asyncio.run(loop.run_turn(log, "write", make))
    assert written == []
    assert any(e.kind == "tool/denied" and e.data["code"] == "owner_denied" for e in log)


def test_hook_can_deny_and_failing_hook_fails_closed() -> None:
    written: list[str] = []
    ctx = Context()
    ctx.load(Plugin("boom", lambda c: c.on("tools/pre-execute", lambda p: 1 / 0)))
    loop = AgentLoop(
        ctx,
        Scripted([call("notes.write", text="x"), "ok"]),
        writer(written),
        approve=lambda n, a: True,
    )
    log = loop.start_session()
    asyncio.run(loop.run_turn(log, "w", make))
    assert written == [] and any(e.data.get("code") == "hook_failed" for e in log)
    ctx.unload("boom")
    loop = AgentLoop(
        ctx,
        Scripted([call("notes.write", text="y"), "ok"]),
        writer(written),
        approve=lambda n, a: True,
    )
    asyncio.run(loop.run_turn(loop.start_session(), "w", make))
    assert written == ["y"]


def test_plugin_effects_unwind_on_unload_and_failed_setup() -> None:
    ctx = Context()
    seen: list[dict] = []

    def setup(c: Context) -> None:
        c.provide("svc", 1)
        c.on("turn/start", seen.append)

    ctx.load(Plugin("p", setup))
    assert ctx.service("svc") == 1
    ctx.unload("p")
    assert ctx.service("svc") is None
    ctx.emit("turn/start", {"turn": 0})
    assert seen == []

    def bad(c: Context) -> None:
        c.provide("half", 1)
        raise RuntimeError

    with pytest.raises(RuntimeError):
        ctx.load(Plugin("bad", bad))
    assert ctx.service("half") is None


def test_keyless_snapshot_replay() -> None:
    loop = AgentLoop(Context(), ReplayGateway.load(SNAPSHOT), builtin_registry())
    log = loop.start_session()
    result = asyncio.run(loop.run_turn(log, "6*7?", make))
    assert result.answer == "It is 42."
    results = [e for e in log if e.kind == "tool/result"]
    assert json.loads(results[0].data["text"]) == {"output": "42"}


def test_recording_gateway(tmp_path: Path) -> None:
    rec = RecordingGateway(Scripted(["hi"]))
    loop = AgentLoop(Context(), rec, ToolRegistry())
    asyncio.run(loop.run_turn(loop.start_session(), "hello", make))
    rec.save(tmp_path / "s.json")
    assert ReplayGateway.load(tmp_path / "s.json")._replies == ["hi"]


def test_step_limit_and_model_failure() -> None:
    loop = AgentLoop(Context(), Scripted([call("clock.now")] * 2), builtin_registry(), max_steps=2)
    assert (
        asyncio.run(loop.run_turn(loop.start_session(), "t", make)).stopped_reason == "step_limit"
    )

    class Down:
        async def complete(self, r):  # type: ignore[no-untyped-def]
            raise ConnectionError

    loop = AgentLoop(Context(), Down(), ToolRegistry())
    r = asyncio.run(loop.run_turn(loop.start_session(), "t", make))
    assert r.stopped_reason == "model_failed" and r.error_code == "ConnectionError"
