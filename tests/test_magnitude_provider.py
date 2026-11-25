from __future__ import annotations

import json

import httpx
import pytest

from max_core.assistant_session import AssistantSession, SessionOptions
from max_core.gateway_trial import TrialConfigurationError
from max_core.magnitude import API_BASE, choose_model, discover_models, magnitude_settings

MODEL = "qwen/test:gguf:q4"


def test_discovery_uses_fixed_local_endpoints() -> None:
    visited = []

    def response(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        if request.url.path == "/health":
            return httpx.Response(200, json={"ready": True})
        assert request.url.path == "/inference/v1/models"
        return httpx.Response(200, json={"data": [{"id": MODEL}, {"id": MODEL}]})

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        assert discover_models(client) == [MODEL]
    assert visited == ["http://127.0.0.1:10100/health", API_BASE + "/models"]
    assert choose_model("", [MODEL]) == MODEL
    with pytest.raises(TrialConfigurationError, match="magnitude_select_model"):
        choose_model("", [MODEL, "another"])
    with pytest.raises(TrialConfigurationError, match="magnitude_model_not_found"):
        choose_model("missing", [MODEL])


def test_discovery_never_follows_a_redirect() -> None:
    visited = []

    def response(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://example.com/"})

    with (
        httpx.Client(transport=httpx.MockTransport(response), follow_redirects=True) as client,
        pytest.raises(TrialConfigurationError, match="magnitude_service_not_ready"),
    ):
        discover_models(client)
    assert visited == ["http://127.0.0.1:10100/health"]


def test_desktop_session_streams_local_text_and_recovers_after_failure(tmp_path) -> None:
    called = 0

    def response(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called += 1
        assert str(request.url) == API_BASE + "/chat/completions"
        body = json.loads(request.content)
        assert body["model"] == MODEL and body["stream"] is True
        assert "tools" not in body and "Authorization" not in request.headers
        if called == 1:
            return httpx.Response(503)
        assert not any(item["content"] == "failed prompt" for item in body["messages"])
        frames = [
            {
                "model": MODEL,
                "choices": [{"index": 0, "delta": {"content": "Use a stable sort."}}],
            },
            {
                "model": MODEL,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        data = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
        return httpx.Response(
            200, text=data + "data: [DONE]\n\n", headers={"Content-Type": "text/event-stream"}
        )

    settings = magnitude_settings({"MAX_MAGNITUDE_MODEL": MODEL})
    options = SessionOptions(tmp_path, None, True, True, True)
    session = AssistantSession(settings, options, transport=httpx.MockTransport(response))
    try:
        assert len(session.registry) == 0
        assert session.ask("failed prompt").stopped_reason == "model_failed"
        assert session.ask("sort scores while preserving ties").answer == "Use a stable sort."
        assert called == 2
    finally:
        session.close()


def test_tools_are_opt_in_and_workspace_reads_only(tmp_path) -> None:
    project = tmp_path / "repository"
    project.mkdir()
    env = {"MAX_MAGNITUDE_MODEL": MODEL, "MAX_MAGNITUDE_TOOLS": "yes"}
    settings = magnitude_settings(env)
    opts = SessionOptions(tmp_path / "data", project, True, True, True)
    session = AssistantSession(settings, opts)
    try:
        assert {tool.name for tool in session.registry} == {"workspace.list", "workspace.read"}
    finally:
        session.close()


def test_native_check_entrypoint_without_loading_qt(monkeypatch, capsys) -> None:
    from max_core import magnitude
    from max_core.native_desktop.__main__ import main

    monkeypatch.setattr("sys.argv", ["max_core.native_desktop", "--check-magnitude"])
    monkeypatch.setattr(magnitude, "check", lambda *, configured: MODEL)
    monkeypatch.delenv("MAX_LOCAL_PROVIDER", raising=False)
    assert main() == 0
    assert MODEL in capsys.readouterr().out


def test_opted_in_model_can_read_a_file_but_cannot_write(tmp_path) -> None:
    root = tmp_path / "fixture-repo"
    root.mkdir()
    source = root / "README.md"
    source.write_text("Fixture line for MAX", encoding="utf-8")
    calls = 0

    def reply(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = json.loads(request.content)
        assert body["stream"] is False
        if calls == 1:
            answer = (
                '<tool_call>{"name":"workspace.read","arguments":{"path":"README.md"}}</tool_call>'
            )
        else:
            assert any("Fixture line for MAX" in row["content"] for row in body["messages"])
            answer = "I found the fixture line."
        return httpx.Response(
            200,
            json={"model": MODEL, "choices": [{"message": {"role": "assistant", "content": answer}}]},
        )

    settings = magnitude_settings({"MAX_MAGNITUDE_MODEL": MODEL, "MAX_MAGNITUDE_TOOLS": "yes"})
    options = SessionOptions(tmp_path / "data", root, True, True, True)
    session = AssistantSession(settings, options, transport=httpx.MockTransport(reply))
    try:
        reply_result = session.ask("Read README.md")
        assert reply_result.answer == "I found the fixture line."
        assert any(step.tool_name == "workspace.read" for step in reply_result.steps)
        assert source.read_text(encoding="utf-8") == "Fixture line for MAX"
    finally:
        session.close()
