"""Tests for MAX Context Breakdown snapshot and /api/context endpoint."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from max_core.context_usage import build_context_snapshot, format_context_summary
from max_core.gateway.contracts import ChatMessage
from max_core.tools import Tool, ToolRegistry, ToolRisk
from max_core.web.app import create_app


def test_build_context_snapshot_categorizes_tools_messages_and_memory() -> None:
    reg = ToolRegistry()
    reg.register(Tool("clock.now", "Get time", ToolRisk.READ_ONLY, (), lambda _a: "now"))
    reg.register(Tool("mcp.github.list", "List repos", ToolRisk.READ_ONLY, (), lambda _a: "[]"))

    class FakeStore:
        def list_memories(self) -> list[dict[str, str]]:
            return [{"key": "owner_name", "text": "Pourush"}]

    sess = SimpleNamespace(
        settings=SimpleNamespace(spec=SimpleNamespace(context_window_tokens=100_000)),
        registry=reg,
        history=[ChatMessage("user", "hello MAX"), ChatMessage("assistant", "Hi there!")],
        store=FakeStore(),
    )

    snap = build_context_snapshot(sess, previous_used_tokens=100, now=1_700_000_000)
    assert snap["total_tokens"] == 100_000
    assert snap["used_tokens"] > 0
    assert snap["counts"]["mcp_tools"] == 1
    assert snap["counts"]["system_tools"] == 1
    assert snap["counts"]["memory_files"] == 1
    assert snap["counts"]["messages"] == 2
    keys = [c["key"] for c in snap["categories"]]
    assert "mcp_tools" in keys and "messages" in keys and "free_space" in keys
    assert "Context" in format_context_summary(snap) or "Using" in format_context_summary(snap)


def test_web_api_context_requires_auth_and_returns_breakdown() -> None:
    app = create_app(
        {"MAX_WEB_PASSCODE": "correct horse battery", "MAX_WEB_INSECURE_COOKIE": "yes"}
    )
    client = TestClient(app)
    assert client.get("/api/context").status_code == 401
    assert client.post("/api/login", json={"passcode": "correct horse battery"}).status_code == 200
    res = client.get("/api/context")
    assert res.status_code == 200
    data = res.json()
    assert "used_percent" in data and "categories" in data and "counts" in data
    assert "context-panel" in client.get("/").text
    assert "showContextPanel" in client.get("/static/orb.js").text
