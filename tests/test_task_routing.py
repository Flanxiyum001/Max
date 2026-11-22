from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from max_core.assistant_session import AssistantSession, SessionOptions
from max_core.gateway_trial import TrialSettings
from max_core.task_routing import classify, route_table
from tests.test_assistant_tools import CLOUD_ENV


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("hey", "quick"),
        ("what's the time", "quick"),
        ("fix this python function, it throws a KeyError", "code"),
        ("why is the sky blue? explain in detail", "reasoning"),
        ("tell me a fun fact about octopuses please", "chat"),
        ("x" * 700, "reasoning"),
    ],
)
def test_classify(text: str, kind: str) -> None:
    assert classify(text) == kind


def test_route_table_auto_defaults_and_overrides() -> None:
    t = route_table("auto", {})
    assert t == {"quick": "auto:fast", "chat": "auto", "code": "auto:smart",
                 "reasoning": "auto:smart"}  # fmt: skip
    t = route_table("auto", {"MAX_ROUTE_CODE": "auto:coding"})
    assert t["code"] == "auto:coding"
    assert set(route_table("auto", {"MAX_ROUTING": "off"}).values()) == {"auto"}
    # A fixed model is never silently swapped unless the owner asks.
    assert set(route_table("gemini-2.5-flash", {}).values()) == {"gemini-2.5-flash"}


def _session(tmp_path: Path, handler) -> AssistantSession:  # type: ignore[no-untyped-def]
    env = dict(CLOUD_ENV, MAX_TRIAL_MODEL="auto")
    settings = TrialSettings.from_env(env)
    return AssistantSession(
        settings,
        SessionOptions(tmp_path, None, False, False, False),
        transport=httpx.MockTransport(handler),
        routes=route_table("auto", {}),
    )


def _ok(model: str, text: str) -> httpx.Response:
    return httpx.Response(200, json={
        "id": "x", "model": model,
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}})  # fmt: skip


def test_session_routes_by_task(tmp_path: Path) -> None:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(json.loads(request.content)["model"])
        return _ok("groq/llama-3.3-70b", "done")

    s = _session(tmp_path, handler)
    try:
        assert s.ask("hey").model == "auto:fast"
        assert s.ask("debug this python script").model == "auto:smart"
        assert s.ask("tell me a story about a fox in the snow").model == "auto"
    finally:
        s.close()
    assert asked == ["auto:fast", "auto:smart", "auto"]


def test_session_falls_back_to_base_and_reports_code(tmp_path: Path) -> None:
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        asked.append(model)
        return httpx.Response(400) if model != "auto" else _ok("x/y", "fallback ok")

    s = _session(tmp_path, handler)
    try:
        r = s.ask("explain why this plan could fail")
        assert r.answer == "fallback ok" and r.model == "auto"
    finally:
        s.close()
    assert asked == ["auto:smart", "auto"]


def test_session_failure_carries_sanitized_code(tmp_path: Path) -> None:
    s = _session(tmp_path, lambda r: httpx.Response(401))
    try:
        r = s.ask("hey")
        assert r.stopped_reason == "model_failed" and r.error_code == "auth_failure"
    finally:
        s.close()
