"""Owner-clicked native local model harness; no hosted requests or tool execution."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from max_core.agent_harness import ActivityKind
from max_core.native_desktop.backend import NativeDesktopCore, NativeDesktopError


def configure_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_TRIAL_DESTINATION", "verified_local")
    monkeypatch.setenv("MAX_TRIAL_LOCAL_EGRESS_REVIEWED", "yes")
    monkeypatch.setenv("MAX_TRIAL_ENDPOINT_LOCAL", "yes")
    monkeypatch.setenv("MAX_TRIAL_MODEL", "synthetic-local")
    monkeypatch.setenv("MAX_TRIAL_ENDPOINT", "http://127.0.0.1:19999/v1/chat/completions")
    monkeypatch.setenv("MAX_TRIAL_CONTEXT_TOKENS", "4096")


def test_local_model_turn_has_real_events_but_never_runs_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_local(monkeypatch)
    observed: list[object] = []
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.url.host == "127.0.0.1"
        assert b"explain the implementation" in request.content
        return httpx.Response(
            200,
            json={
                "model": "synthetic-local",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Fixture response, not a completed coding job.",
                            "tool_calls": [
                                {
                                    "id": "fixture-call-1",
                                    "function": {
                                        "name": "repo.read",
                                        "arguments": '{"path":"secret_input_not_logged"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    backend = NativeDesktopCore()
    try:
        result = backend.ask_local("explain the implementation", on_activity=observed.append, client=client)
        assert result.state == "review_required"
        assert result.proposed_tools == ("repo.read",)
        assert "Fixture response" in result.text
        assert "secret_input_not_logged" not in repr(result)
        assert len(calls) == 1
        assert [event.kind for event in observed][-2:] == [
            ActivityKind.TOOL_PROPOSED,
            ActivityKind.REVIEW_REQUIRED,
        ]
        assert "secret_input_not_logged" not in repr(observed)
    finally:
        backend.close()
        asyncio.run(client.aclose())


def test_unconfigured_local_trial_denied_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_TRIAL_DESTINATION", "unknown")
    backend = NativeDesktopCore()
    try:
        with pytest.raises(NativeDesktopError, match="local_model_not_configured"):
            backend.ask_local("Please plan the feature")
    finally:
        backend.close()


def test_cloud_trial_configuration_is_denied_even_if_explicitly_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_TRIAL_DESTINATION", "cloud")
    monkeypatch.setenv("MAX_TRIAL_CLOUD_ALLOWED", "yes")
    monkeypatch.setenv("MAX_TRIAL_MODEL", "synthetic-cloud")
    monkeypatch.setenv("MAX_TRIAL_ENDPOINT_LOCAL", "no")
    monkeypatch.setenv("MAX_TRIAL_ENDPOINT", "https://api.example.invalid/v1/chat/completions")
    monkeypatch.setenv("MAX_TRIAL_CONTEXT_TOKENS", "4096")
    monkeypatch.setenv("MAX_TRIAL_API_KEY", "synthetic-test-credential")
    monkeypatch.setenv("MAX_TRIAL_MAX_COST_USD", "1")
    monkeypatch.setenv("MAX_TRIAL_INPUT_USD_PER_MILLION", "1")
    monkeypatch.setenv("MAX_TRIAL_OUTPUT_USD_PER_MILLION", "1")
    backend = NativeDesktopCore()
    try:
        with pytest.raises(NativeDesktopError, match="local_model_only"):
            backend.ask_local("Never contact the cloud")
    finally:
        backend.close()


def test_invalid_prompt_denied_before_local_model_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure_local(monkeypatch)
    backend = NativeDesktopCore()
    try:
        for value in ("", "  ", "x" * 2001, None, 42):
            with pytest.raises(NativeDesktopError, match="invalid_model_prompt"):
                backend.ask_local(value)  # type: ignore[arg-type]
    finally:
        backend.close()
