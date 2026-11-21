"""Pre-staged E2E tests for live cloud text chat (F4) and Config+Chat (T3-03)."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from max_core.gateway.adapters import HTTPAdapter
from max_core.gateway.contracts import (
    ChatMessage,
    Dialect,
    GatewayError,
    GatewayRequest,
    InferenceDestination,
    ModelSpec,
    Privacy,
    TokenUsage,
)
from max_core.gateway.router import Gateway
from max_core.local_config import DesktopConfig, load_desktop_config
from max_core.native_desktop.backend import NativeDesktopCore
from max_core.persona import ReplyMode, persona_message, with_persona
from tests.e2e.conftest import (
    MockLLMServer,
    create_mocked_max_window,
    unused_loopback_port,
)


def _make_chat_service(
    config: DesktopConfig,
    server: MockLLMServer | None = None,
) -> tuple[Any, httpx.AsyncClient | None]:
    from max_core.assistant_chat import AssistantChatService

    sig = inspect.signature(AssistantChatService.__init__)
    kwargs: dict[str, Any] = {}
    client: httpx.AsyncClient | None = None
    if server is not None:
        transport = server.as_mock_transport()
        if "transport" in sig.parameters:
            kwargs["transport"] = transport
        elif "http_client" in sig.parameters:
            client = httpx.AsyncClient(transport=transport)
            kwargs["http_client"] = client
        elif "client" in sig.parameters:
            client = httpx.AsyncClient(transport=transport)
            kwargs["client"] = client
    params = list(sig.parameters)
    if "config" in sig.parameters:
        kwargs["config"] = config
        service = AssistantChatService(**kwargs)
    elif len(params) > 1:
        service = AssistantChatService(config, **kwargs)
    else:
        service = AssistantChatService(**kwargs)
    return service, client


def _send_chat(
    service: Any,
    user_text: str,
    *,
    mode: ReplyMode | None = None,
) -> Any:
    if mode is not None and hasattr(service, "set_reply_mode"):
        service.set_reply_mode(mode)
    sig = inspect.signature(service.send_message)
    kwargs: dict[str, Any] = {}
    if mode is not None:
        if "mode" in sig.parameters:
            kwargs["mode"] = mode
        elif "reply_mode" in sig.parameters:
            kwargs["reply_mode"] = mode
    result = service.send_message(user_text, **kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _pump_until(app: Any, predicate: Callable[[], bool], *, timeout_s: float = 4.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    app.processEvents()
    assert predicate(), "Timed out waiting for Qt chat UI update"


# ---------------------------------------------------------------------------
# Tier 1 — F4: Live Cloud Text Chat Happy Paths
# ---------------------------------------------------------------------------


def test_t1_f4_01_openai_chat_round_trip_with_persona(
    tmp_path: Path,
    mock_llm_server: MockLLMServer,
) -> None:
    """T1-F4-01: OpenAI chat turn sends Bearer auth + persona and returns ChatTurnResult."""
    mock_llm_server.queue_text(
        "Good morning. Ready to help with your local tasks.",
        usage={"prompt_tokens": 120, "completion_tokens": 18},
    )
    cfg = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "chat_openai.sqlite3"),
            "MAX_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-openai-e2e-1234567890",
            "MAX_OPENAI_MODEL": "gpt-4o-mini",
            "MAX_OPENAI_INPUT_USD_PER_MILLION": "0.15",
            "MAX_OPENAI_OUTPUT_USD_PER_MILLION": "0.60",
            "MAX_COST_USD": "0.05",
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    service, client = _make_chat_service(cfg, mock_llm_server)
    try:
        result = _send_chat(service, "Hello MAX")
    finally:
        if client is not None:
            asyncio.run(client.aclose())

    assert result.ok is True
    assert result.provider == "openai"
    assert result.model_id == "gpt-4o-mini"
    assert result.error_code is None
    assert "Ready to help with your local tasks." in result.reply
    assert result.estimated_cost_usd is not None and 0 <= result.estimated_cost_usd <= 0.05

    assert len(mock_llm_server.requests) == 1
    req = mock_llm_server.requests[0]
    assert req.path.endswith("/v1/chat/completions")
    assert req.headers.get("authorization") == "Bearer sk-test-openai-e2e-1234567890"
    assert req.json_body is not None
    assert req.json_body["model"] == "gpt-4o-mini"
    messages = req.json_body["messages"]
    assert messages[0]["role"] == "system"
    assert "You are MAX" in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "Hello MAX"}


def test_t1_f4_02_anthropic_chat_round_trip_with_persona(
    tmp_path: Path,
    mock_llm_server: MockLLMServer,
) -> None:
    """T1-F4-02: Anthropic chat turn sends x-api-key, anthropic-version, and top-level system."""
    mock_llm_server.queue_text("Local status is nominal and ready.")
    cfg = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "chat_anth.sqlite3"),
            "MAX_LLM_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "sk-ant-test-e2e-1234567890",
            "MAX_ANTHROPIC_MODEL": "claude-sonnet-4-5",
            "MAX_ANTHROPIC_INPUT_USD_PER_MILLION": "3.0",
            "MAX_ANTHROPIC_OUTPUT_USD_PER_MILLION": "15.0",
            "MAX_COST_USD": "0.10",
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    service, client = _make_chat_service(cfg, mock_llm_server)
    try:
        result = _send_chat(service, "Summarise status")
    finally:
        if client is not None:
            asyncio.run(client.aclose())

    assert result.ok is True
    assert result.provider == "anthropic"
    assert result.model_id == "claude-sonnet-4-5"
    assert result.error_code is None
    assert "Local status is nominal and ready." in result.reply

    assert len(mock_llm_server.requests) == 1
    req = mock_llm_server.requests[0]
    assert req.path.endswith("/v1/messages")
    assert req.headers.get("x-api-key") == "sk-ant-test-e2e-1234567890"
    assert req.headers.get("anthropic-version") == "2023-06-01"
    assert req.json_body is not None
    assert "You are MAX" in str(req.json_body.get("system", ""))
    assert all(m["role"] != "system" for m in req.json_body["messages"])
    assert req.json_body["messages"][-1] == {"role": "user", "content": "Summarise status"}


def test_t1_f4_03_multi_turn_conversation_preserves_history_after_persona(
    tmp_path: Path,
    mock_llm_server: MockLLMServer,
) -> None:
    """T1-F4-03: Multi-turn conversation forwards prior user/assistant turns after persona."""
    mock_llm_server.queue_text("Noted: your project is Alpha.")
    mock_llm_server.queue_text("You named it Alpha.")
    mock_llm_server.queue_text("Next step: run the local persistence checks.")
    cfg = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "chat_multiturn.sqlite3"),
            "MAX_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-multi-1234567890",
            "MAX_OPENAI_INPUT_USD_PER_MILLION": "0.15",
            "MAX_OPENAI_OUTPUT_USD_PER_MILLION": "0.60",
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    service, client = _make_chat_service(cfg, mock_llm_server)
    try:
        r1 = _send_chat(service, "My project is Alpha")
        r2 = _send_chat(service, "What did I name it?")
        r3 = _send_chat(service, "Give next step")
    finally:
        if client is not None:
            asyncio.run(client.aclose())

    assert r1.ok and r2.ok and r3.ok
    assert "Alpha" in r2.reply
    assert len(mock_llm_server.requests) == 3

    third_body = mock_llm_server.requests[2].json_body
    assert third_body is not None
    msgs = third_body["messages"]
    assert msgs[0]["role"] == "system"
    assert [m["role"] for m in msgs[1:]] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert msgs[1]["content"] == "My project is Alpha"
    assert msgs[2]["content"] == "Noted: your project is Alpha."
    assert msgs[3]["content"] == "What did I name it?"
    assert msgs[4]["content"] == "You named it Alpha."
    assert msgs[5]["content"] == "Give next step"


def test_t1_f4_04_reply_mode_work_and_focus_shape_persona_prompt(
    tmp_path: Path,
    mock_llm_server: MockLLMServer,
) -> None:
    """T1-F4-04: ReplyMode.WORK and ReplyMode.FOCUS select distinct trusted persona prompts."""
    mock_llm_server.queue_text("Work summary ready.")
    mock_llm_server.queue_text("Focused reply.")

    async def _run() -> tuple[str, str]:
        spec = ModelSpec(
            provider_id="openai",
            model_id="gpt-4o-mini",
            dialect=Dialect.OPENAI_CHAT,
            endpoint=mock_llm_server.openai_endpoint,
            is_local=True,
            context_tokens=16384,
            inference_destination=InferenceDestination.CLOUD,
            input_usd_per_million=Decimal("0.15"),
            output_usd_per_million=Decimal("0.60"),
            api_key="sk-test-mode-1234567890",
        )
        async with httpx.AsyncClient(trust_env=False) as http_client:
            gw = Gateway([spec], HTTPAdapter(http_client))
            req_work = with_persona(
                GatewayRequest(
                    request_id="req-work-1",
                    messages=(ChatMessage("user", "Report status"),),
                    privacy=Privacy.INTERNAL,
                    max_input_tokens=4096,
                    max_output_tokens=256,
                    max_cost_usd=Decimal("0.05"),
                    cloud_allowed=True,
                ),
                mode=ReplyMode.WORK,
            )
            req_focus = with_persona(
                GatewayRequest(
                    request_id="req-focus-1",
                    messages=(ChatMessage("user", "Status?"),),
                    privacy=Privacy.INTERNAL,
                    max_input_tokens=4096,
                    max_output_tokens=256,
                    max_cost_usd=Decimal("0.05"),
                    cloud_allowed=True,
                ),
                mode=ReplyMode.FOCUS,
            )
            resp_work = await gw.complete(req_work)
            resp_focus = await gw.complete(req_focus)
            return resp_work.content, resp_focus.content

    out_work, out_focus = asyncio.run(_run())
    assert out_work == "Work summary ready."
    assert out_focus == "Focused reply."
    assert len(mock_llm_server.requests) == 2

    work_sys = mock_llm_server.requests[0].json_body["messages"][0]["content"]  # type: ignore[index]
    focus_sys = mock_llm_server.requests[1].json_body["messages"][0]["content"]  # type: ignore[index]
    assert persona_message(ReplyMode.WORK).content == work_sys
    assert persona_message(ReplyMode.FOCUS).content == focus_sys
    assert work_sys != focus_sys


def test_t1_f4_05_gateway_stream_and_cost_accounting_within_budget(
    tmp_path: Path,
    mock_llm_server: MockLLMServer,
) -> None:
    """T1-F4-05: Token usage & cost accounting stay accurate and within MAX_COST_USD."""
    mock_llm_server.queue_text(
        "Estimated cost check succeeded.",
        usage={"prompt_tokens": 1_000, "completion_tokens": 500},
    )
    cfg = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "chat_cost.sqlite3"),
            "MAX_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-cost-1234567890",
            "MAX_OPENAI_INPUT_USD_PER_MILLION": "1.0",
            "MAX_OPENAI_OUTPUT_USD_PER_MILLION": "2.0",
            "MAX_COST_USD": "0.05",
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    service, client = _make_chat_service(cfg, mock_llm_server)
    try:
        result = _send_chat(service, "Calculate usage cost")
    finally:
        if client is not None:
            asyncio.run(client.aclose())

    assert result.ok is True
    assert "Estimated cost check succeeded." in result.reply
    expected_cost = (1_000 * 1.0 + 500 * 2.0) / 1_000_000.0
    assert result.estimated_cost_usd is not None
    assert result.estimated_cost_usd == pytest.approx(expected_cost, rel=1e-6)

    mock_llm_server.queue_text(
        "Streamed OpenAI reply.",
        usage={"prompt_tokens": 100, "completion_tokens": 50},
        stream=True,
    )
    mock_llm_server.queue_text(
        "Streamed Anthropic reply.",
        usage={"input_tokens": 80, "output_tokens": 40},
        stream=True,
    )

    async def _verify_streams() -> None:
        openai_spec = ModelSpec(
            provider_id="openai",
            model_id="gpt-4o-mini",
            dialect=Dialect.OPENAI_CHAT,
            endpoint="https://api.openai.com/v1/chat/completions",
            is_local=False,
            context_tokens=16384,
            inference_destination=InferenceDestination.CLOUD,
            input_usd_per_million=Decimal("1.0"),
            output_usd_per_million=Decimal("2.0"),
            api_key="sk-test-stream-openai-123456",
        )
        anthropic_spec = ModelSpec(
            provider_id="anthropic",
            model_id="claude-sonnet-4-5",
            dialect=Dialect.ANTHROPIC_MESSAGES,
            endpoint="https://api.anthropic.com/v1/messages",
            is_local=False,
            context_tokens=16384,
            inference_destination=InferenceDestination.CLOUD,
            input_usd_per_million=Decimal("3.0"),
            output_usd_per_million=Decimal("15.0"),
            api_key="sk-ant-stream-123456",
        )
        async with httpx.AsyncClient(
            trust_env=False, transport=mock_llm_server.as_mock_transport()
        ) as http_client:
            oa_gw = Gateway([openai_spec], HTTPAdapter(http_client))
            oa_req = with_persona(
                GatewayRequest(
                    request_id="req-stream-oa",
                    messages=(ChatMessage("user", "Stream OpenAI"),),
                    privacy=Privacy.INTERNAL,
                    max_input_tokens=4096,
                    max_output_tokens=256,
                    max_cost_usd=Decimal("0.05"),
                    cloud_allowed=True,
                )
            )
            oa_events = [ev async for ev in oa_gw.stream(oa_req)]

            an_gw = Gateway([anthropic_spec], HTTPAdapter(http_client))
            an_req = with_persona(
                GatewayRequest(
                    request_id="req-stream-an",
                    messages=(ChatMessage("user", "Stream Anthropic"),),
                    privacy=Privacy.INTERNAL,
                    max_input_tokens=4096,
                    max_output_tokens=256,
                    max_cost_usd=Decimal("0.05"),
                    cloud_allowed=True,
                )
            )
            an_events = [ev async for ev in an_gw.stream(an_req)]

        for events, expected_text, expected_usage, expected_finish in (
            (oa_events, "Streamed OpenAI reply.", TokenUsage(100, 50), "stop"),
            (an_events, "Streamed Anthropic reply.", TokenUsage(80, 40), "end_turn"),
        ):
            kinds = [e.kind for e in events]
            assert kinds[0] == "start"
            assert kinds[-2:] == ["usage", "complete"]
            assert len(kinds[1:-2]) >= 1
            assert all(k == "content_delta" for k in kinds[1:-2])
            assert [e.sequence for e in events] == list(range(len(events)))
            assert "".join(e.text for e in events if e.kind == "content_delta") == expected_text
            assert events[-2].usage == expected_usage
            assert events[-1].finish_reason == expected_finish

    asyncio.run(_verify_streams())


def test_t1_f4_06_max_window_chat_ui_submits_offscreen_and_shows_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desktop_core_factory: Callable[..., NativeDesktopCore],
    mock_llm_server: MockLLMServer,
) -> None:
    """T1-F4-06: MaxWindow offscreen chat submits message off-thread and renders reply + badge."""
    from PySide6 import QtWidgets

    from max_core.native_desktop.qt_ui import MaxWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    mock_llm_server.queue_text("Desktop UI chat response verified.")

    cfg = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "ui_chat.sqlite3"),
            "MAX_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-ui-chat-1234567890",
            "MAX_OPENAI_MODEL": "gpt-4o-mini",
            "MAX_OPENAI_INPUT_USD_PER_MILLION": "0.15",
            "MAX_OPENAI_OUTPUT_USD_PER_MILLION": "0.60",
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = desktop_core_factory(config=cfg)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = create_mocked_max_window(MaxWindow, core, mock_llm_server, monkeypatch)
    try:
        window.show()
        _pump_until(app, lambda: "checking" not in window.system_storage.text().lower())

        chat_input = getattr(window, "chat_input", None) or window.command
        send_chat = getattr(window, "send_chat", None)
        assert callable(send_chat), "M5 MaxWindow must expose send_chat()"
        chat_input.setPlainText("Hello from Qt chat UI")
        send_chat()

        def _reply_visible() -> bool:
            transcript = getattr(window, "chat_transcript", None) or getattr(
                window, "chat_history", None
            )
            if transcript is None:
                return False
            if hasattr(transcript, "toPlainText"):
                text = str(transcript.toPlainText())
            elif hasattr(transcript, "count"):
                text = "\n".join(transcript.item(i).text() for i in range(transcript.count()))
            else:
                text = str(transcript.text())
            return "Desktop UI chat response verified." in text

        _pump_until(app, _reply_visible)
        assert len(mock_llm_server.requests) == 1
    finally:
        window.close()
        app.processEvents()


# ---------------------------------------------------------------------------
# Tier 2 — F4: Missing/Invalid Keys, HTTP Errors, Privacy Guard, Budgets
# ---------------------------------------------------------------------------


def test_t2_f4_01_missing_api_key_returns_sanitized_missing_api_key_error(
    tmp_path: Path,
    mock_llm_server: MockLLMServer,
) -> None:
    """T2-F4-01: Missing API key yields ok=False, error_code='missing_api_key', 0 HTTP calls."""
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "chat_nokey.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    assert cfg.active_provider is None
    service, client = _make_chat_service(cfg, mock_llm_server)
    try:
        result = _send_chat(service, "Hello without key")
    finally:
        if client is not None:
            asyncio.run(client.aclose())

    assert result.ok is False
    assert result.error_code == "missing_api_key"
    assert len(mock_llm_server.requests) == 0


def test_t2_f4_02_http_401_and_403_return_auth_failed_without_key_leak(
    tmp_path: Path,
    mock_llm_server: MockLLMServer,
) -> None:
    """T2-F4-02: HTTP 401/403 map to auth_failed and never leak the configured API key."""
    secret_key = "sk-e2e-invalid-secret-never-echo-999999"
    mock_llm_server.queue_error(401, body=b'{"error": {"message": "bad auth"}}')
    mock_llm_server.queue_error(403, body=b'{"error": {"message": "forbidden"}}')

    cfg = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "chat_401.sqlite3"),
            "MAX_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": secret_key,
            "MAX_OPENAI_INPUT_USD_PER_MILLION": "0.15",
            "MAX_OPENAI_OUTPUT_USD_PER_MILLION": "0.60",
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    service, client = _make_chat_service(cfg, mock_llm_server)
    try:
        r401 = _send_chat(service, "Try with 401")
        r403 = _send_chat(service, "Try with 403")
    finally:
        if client is not None:
            asyncio.run(client.aclose())

    for res in (r401, r403):
        assert res.ok is False
        assert res.error_code == "auth_failed"
        assert secret_key not in res.reply


def test_t2_f4_03_rate_limit_503_connect_error_and_timeout_degrade_cleanly(
    tmp_path: Path,
    mock_llm_server: MockLLMServer,
) -> None:
    """T2-F4-03: HTTP 429, 503, ConnectError, and Timeout return sanitized errors cleanly."""
    mock_llm_server.queue_error(429)
    mock_llm_server.queue_error(503)
    mock_llm_server.queue_error(0, raise_connect_error=True)
    mock_llm_server.queue_error(0, raise_timeout=True)

    cfg = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "chat_net.sqlite3"),
            "MAX_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-net-1234567890",
            "MAX_OPENAI_INPUT_USD_PER_MILLION": "0.15",
            "MAX_OPENAI_OUTPUT_USD_PER_MILLION": "0.60",
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    service, client = _make_chat_service(cfg, mock_llm_server)
    try:
        r429 = _send_chat(service, "Rate limit check")
        r503 = _send_chat(service, "Unavailable check")
        r_conn = _send_chat(service, "Connect error check")
        r_time = _send_chat(service, "Timeout check")
    finally:
        if client is not None:
            asyncio.run(client.aclose())

    assert r429.ok is False and r429.error_code == "rate_limited"
    assert r503.ok is False and r503.error_code in {"provider_unavailable", "network_error"}
    assert r_conn.ok is False and r_conn.error_code in {"network_error", "provider_unavailable"}
    assert r_time.ok is False and r_time.error_code in {"timeout", "network_error"}

    # Also verify raw unreachable TCP port fails cleanly via local Gateway without unhandled error.
    dead_port = unused_loopback_port()

    async def _check_dead_port() -> None:
        spec = ModelSpec(
            provider_id="openai",
            model_id="gpt-4o-mini",
            dialect=Dialect.OPENAI_CHAT,
            endpoint=f"http://127.0.0.1:{dead_port}/v1/chat/completions",
            is_local=True,
            context_tokens=16384,
            inference_destination=InferenceDestination.CLOUD,
            input_usd_per_million=Decimal("0.15"),
            output_usd_per_million=Decimal("0.60"),
            api_key="sk-test-dead-1234567890",
        )
        async with httpx.AsyncClient(trust_env=False) as http_client:
            gw = Gateway([spec], HTTPAdapter(http_client))
            with pytest.raises(GatewayError, match="provider_unavailable"):
                await gw.complete(
                    GatewayRequest(
                        request_id="req-dead-port",
                        messages=(ChatMessage("user", "Unreachable loopback"),),
                        privacy=Privacy.INTERNAL,
                        max_input_tokens=4096,
                        max_output_tokens=128,
                        max_cost_usd=Decimal("0.05"),
                        cloud_allowed=True,
                    )
                )

    asyncio.run(_check_dead_port())


def test_t2_f4_04_privacy_guard_blocks_credentials_with_zero_http_calls(
    tmp_path: Path,
    mock_llm_server: MockLLMServer,
) -> None:
    """T2-F4-04: High-confidence secrets in prompts are blocked before any HTTP egress."""
    cfg = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "chat_privacy.sqlite3"),
            "MAX_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-privacy-1234567890",
            "MAX_OPENAI_INPUT_USD_PER_MILLION": "0.15",
            "MAX_OPENAI_OUTPUT_USD_PER_MILLION": "0.60",
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    sensitive_prompts = [
        "Please store sk-proj-abcdefghijklmnopqrstuvwxyz123456",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA",
        "Authorization: Bearer supersecretbearertoken12345678",
        "DATABASE_URL=postgresql+psycopg://owner:secretpw@127.0.0.1:5432/max",
    ]
    service, client = _make_chat_service(cfg, mock_llm_server)
    try:
        for prompt in sensitive_prompts:
            res = _send_chat(service, prompt)
            assert res.ok is False
            assert res.error_code in {"egress_blocked", "cloud_sensitive_content_blocked"}
            assert "supersecretbearertoken" not in res.reply
            assert "sk-proj-" not in res.reply
    finally:
        if client is not None:
            asyncio.run(client.aclose())

    assert len(mock_llm_server.requests) == 0


def test_t2_f4_05_malformed_json_missing_choices_and_model_mismatch_rejected(
    tmp_path: Path,
    mock_llm_server: MockLLMServer,
) -> None:
    """T2-F4-05: Malformed JSON, missing choices, and mismatched model ID fail cleanly."""
    mock_llm_server.queue_malformed(b"{bad json")
    mock_llm_server.queue_malformed(json.dumps({"id": "1", "choices": []}).encode("utf-8"))
    mock_llm_server.queue_model_mismatch("wrong-model-id")

    cfg = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "chat_bad_resp.sqlite3"),
            "MAX_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-badresp-1234567890",
            "MAX_OPENAI_MODEL": "gpt-4o-mini",
            "MAX_OPENAI_INPUT_USD_PER_MILLION": "0.15",
            "MAX_OPENAI_OUTPUT_USD_PER_MILLION": "0.60",
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    service, client = _make_chat_service(cfg, mock_llm_server)
    try:
        r_json = _send_chat(service, "Malformed JSON")
        r_choices = _send_chat(service, "Missing choices")
        r_model = _send_chat(service, "Wrong model")
    finally:
        if client is not None:
            asyncio.run(client.aclose())

    assert r_json.ok is False
    assert r_choices.ok is False
    assert r_model.ok is False


def test_t2_f4_06_token_and_cost_ceilings_block_before_egress(
    tmp_path: Path,
    mock_llm_server: MockLLMServer,
) -> None:
    """T2-F4-06: Exceeding max_cost_usd or max_input_tokens blocks before HTTP egress."""
    cfg = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "chat_budget.sqlite3"),
            "MAX_LLM_PROVIDER": "openai",
            "OPENAI_API_KEY": "sk-test-budget-1234567890",
            "MAX_OPENAI_INPUT_USD_PER_MILLION": "50.0",
            "MAX_OPENAI_OUTPUT_USD_PER_MILLION": "100.0",
            "MAX_COST_USD": "0.0001",
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    service, client = _make_chat_service(cfg, mock_llm_server)
    try:
        res = _send_chat(service, "This request exceeds the tiny 0.0001 budget ceiling.")
    finally:
        if client is not None:
            asyncio.run(client.aclose())

    assert res.ok is False
    assert len(mock_llm_server.requests) == 0

    async def _check_tight_tokens() -> None:
        spec = ModelSpec(
            provider_id="openai",
            model_id="gpt-4o-mini",
            dialect=Dialect.OPENAI_CHAT,
            endpoint=mock_llm_server.openai_endpoint,
            is_local=True,
            context_tokens=16384,
            inference_destination=InferenceDestination.CLOUD,
            input_usd_per_million=Decimal("0.15"),
            output_usd_per_million=Decimal("0.60"),
            api_key="sk-test-budget-1234567890",
        )
        async with httpx.AsyncClient(trust_env=False) as http_client:
            gw = Gateway([spec], HTTPAdapter(http_client))
            with pytest.raises(ValueError):
                GatewayRequest(
                    request_id="req-too-small",
                    messages=(ChatMessage("user", "A" * 200),),
                    privacy=Privacy.INTERNAL,
                    max_input_tokens=64,
                    max_output_tokens=64,
                    max_cost_usd=Decimal("0.05"),
                    cloud_allowed=True,
                )
            with pytest.raises(GatewayError):
                await gw.complete(
                    GatewayRequest(
                        request_id="req-budget",
                        messages=(ChatMessage("user", "Hello"),),
                        privacy=Privacy.INTERNAL,
                        max_input_tokens=4096,
                        max_output_tokens=4096,
                        max_cost_usd=Decimal("0.0001"),
                        cloud_allowed=True,
                    )
                )

    asyncio.run(_check_tight_tokens())
    assert len(mock_llm_server.requests) == 0


# ---------------------------------------------------------------------------
# Tier 3 — F1 + F4: Provider Switch Across Config Override
# ---------------------------------------------------------------------------


def test_t3_03_provider_switch_openai_to_anthropic_across_config_override(
    tmp_path: Path,
    mock_llm_server: MockLLMServer,
) -> None:
    """T3-03: Switching MAX_LLM_PROVIDER from OpenAI (.env) to Anthropic (env) switches dialect."""
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "\n".join(
            [
                "MAX_LLM_PROVIDER=openai",
                "OPENAI_API_KEY=sk-openai-switch-123456",
                "ANTHROPIC_API_KEY=sk-ant-switch-123456",
            ]
        ),
        encoding="utf-8",
    )
    mock_llm_server.queue_text("OpenAI session active.")
    mock_llm_server.queue_text("Anthropic session active.")

    cfg_openai = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "switch.sqlite3")},
        dotenv_path=dotenv_file,
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    svc1, client1 = _make_chat_service(cfg_openai, mock_llm_server)
    try:
        res1 = _send_chat(svc1, "Who is active?")
    finally:
        if client1 is not None:
            asyncio.run(client1.aclose())

    cfg_anth = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "switch.sqlite3"),
            "MAX_LLM_PROVIDER": "anthropic",
        },
        dotenv_path=dotenv_file,
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    svc2, client2 = _make_chat_service(cfg_anth, mock_llm_server)
    try:
        res2 = _send_chat(svc2, "Who is active now?")
    finally:
        if client2 is not None:
            asyncio.run(client2.aclose())

    assert res1.ok is True and res1.provider == "openai"
    assert res2.ok is True and res2.provider == "anthropic"
    assert len(mock_llm_server.requests) == 2
    assert mock_llm_server.requests[0].path.endswith("/v1/chat/completions")
    assert "authorization" in mock_llm_server.requests[0].headers
    assert mock_llm_server.requests[1].path.endswith("/v1/messages")
    assert mock_llm_server.requests[1].headers.get("x-api-key") == "sk-ant-switch-123456"
