"""Shared E2E fixtures, process isolation, mock LLM HTTP/transport server, and voice doubles."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import queue
import socket
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from max_core.local_config import DesktopConfig
from max_core.native_desktop.backend import NativeDesktopCore
from max_core.store import TaskStore

REPO_ROOT = Path(__file__).resolve().parents[2]

_STRIPPED_ENV_PREFIXES = ("MAX_", "OPENAI_", "ANTHROPIC_")
_STRIPPED_PROXY_KEYS = frozenset({"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"})


def build_clean_desktop_env(
    base_dir: Path,
    *,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an isolated process environment stripped of host MAX/LLM variables."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith(_STRIPPED_ENV_PREFIXES)
        and key.upper() not in _STRIPPED_PROXY_KEYS
    }
    local_app = base_dir / "localappdata"
    xdg_data = base_dir / "xdg_data"
    home_dir = base_dir / "home"
    env.update(
        {
            "QT_QPA_PLATFORM": "offscreen",
            "LOCALAPPDATA": str(local_app),
            "XDG_DATA_HOME": str(xdg_data),
            "HOME": str(home_dir),
            "USERPROFILE": str(home_dir),
            "PYTHONUTF8": "1",
        }
    )
    if overrides:
        env.update(overrides)
    return env


def unused_loopback_port() -> int:
    """Bind an ephemeral loopback port briefly and return it closed for ConnectError tests."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True, slots=True)
class CapturedHTTPRequest:
    method: str
    path: str
    headers: dict[str, str]
    body_bytes: bytes
    json_body: dict[str, Any] | None


@dataclass(slots=True)
class ScriptedResponse:
    status_code: int = 200
    text: str = "Hello from mock MAX assistant."
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] | None = None
    model: str | None = None
    stream: bool | None = None
    raw_body: bytes | None = None
    finish_reason: str | None = None
    raise_connect_error: bool = False
    raise_timeout: bool = False
    headers: dict[str, str] = field(default_factory=dict)


def _resolve_advertised_tool_name(
    name: str,
    req_json: dict[str, Any],
    *,
    dialect: str,
) -> str:
    """Resolve queued canonical tool names (e.g. 'dev.add') to wire names advertised in request."""
    tools = req_json.get("tools", [])
    advertised: set[str] = set()
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if dialect == "openai":
                fn = tool.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    advertised.add(fn["name"])
            else:
                if isinstance(tool.get("name"), str):
                    advertised.add(tool["name"])
    if name not in advertised:
        wire_candidate = name.replace(".", "_")
        if wire_candidate in advertised:
            return wire_candidate
    return name


def _encode_openai_json(
    req_json: dict[str, Any],
    spec: ScriptedResponse,
) -> bytes:
    model_id = spec.model if spec.model is not None else str(req_json.get("model", "gpt-4o-mini"))
    usage = spec.usage or {"prompt_tokens": 24, "completion_tokens": 12}
    message: dict[str, Any] = {"role": "assistant", "content": spec.text}
    if spec.tool_calls:
        message["tool_calls"] = [
            {
                "id": str(tc.get("id", f"call_{idx}")),
                "type": "function",
                "function": {
                    "name": _resolve_advertised_tool_name(
                        str(tc["name"]), req_json, dialect="openai"
                    ),
                    "arguments": json.dumps(tc.get("arguments", {})),
                },
            }
            for idx, tc in enumerate(spec.tool_calls)
        ]
        finish = spec.finish_reason or "tool_calls"
    else:
        finish = spec.finish_reason or "stop"
    payload = {
        "id": "chatcmpl-mock-1",
        "object": "chat.completion",
        "model": model_id,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": usage,
    }
    return json.dumps(payload).encode("utf-8")


def _encode_openai_sse(
    req_json: dict[str, Any],
    spec: ScriptedResponse,
) -> bytes:
    model_id = spec.model if spec.model is not None else str(req_json.get("model", "gpt-4o-mini"))
    usage = spec.usage or {"prompt_tokens": 24, "completion_tokens": 12}
    frames: list[str] = []
    if spec.text:
        midpoint = max(1, len(spec.text) // 2)
        for part in (spec.text[:midpoint], spec.text[midpoint:]):
            if not part:
                continue
            chunk = {
                "id": "chatcmpl-mock-stream",
                "model": model_id,
                "choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}],
            }
            frames.append(f"data: {json.dumps(chunk)}\n\n")
    if spec.tool_calls:
        for idx, tc in enumerate(spec.tool_calls):
            args_str = json.dumps(tc.get("arguments", {}))
            chunk = {
                "id": "chatcmpl-mock-stream",
                "model": model_id,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": idx,
                                    "id": str(tc.get("id", f"call_{idx}")),
                                    "function": {
                                        "name": _resolve_advertised_tool_name(
                                            str(tc["name"]), req_json, dialect="openai"
                                        ),
                                        "arguments": args_str,
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
            frames.append(f"data: {json.dumps(chunk)}\n\n")
    finish = spec.finish_reason or ("tool_calls" if spec.tool_calls else "stop")
    frames.append(
        f"data: {json.dumps({'id': 'chatcmpl-mock-stream', 'model': model_id, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]})}\n\n"
    )
    frames.append(
        f"data: {json.dumps({'id': 'chatcmpl-mock-stream', 'model': model_id, 'choices': [], 'usage': usage})}\n\n"
    )
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode("utf-8")


def _encode_anthropic_json(
    req_json: dict[str, Any],
    spec: ScriptedResponse,
) -> bytes:
    model_id = (
        spec.model if spec.model is not None else str(req_json.get("model", "claude-sonnet-4-5"))
    )
    usage = spec.usage or {"input_tokens": 24, "output_tokens": 12}
    blocks: list[dict[str, Any]] = []
    if spec.text:
        blocks.append({"type": "text", "text": spec.text})
    for idx, tc in enumerate(spec.tool_calls):
        blocks.append(
            {
                "type": "tool_use",
                "id": str(tc.get("id", f"toolu_{idx}")),
                "name": _resolve_advertised_tool_name(
                    str(tc["name"]), req_json, dialect="anthropic"
                ),
                "input": dict(tc.get("arguments", {})),
            }
        )
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    stop_reason = spec.finish_reason or ("tool_use" if spec.tool_calls else "end_turn")
    payload = {
        "id": "msg_mock_1",
        "type": "message",
        "role": "assistant",
        "model": model_id,
        "content": blocks,
        "stop_reason": stop_reason,
        "usage": usage,
    }
    return json.dumps(payload).encode("utf-8")


def _encode_anthropic_sse(
    req_json: dict[str, Any],
    spec: ScriptedResponse,
) -> bytes:
    model_id = (
        spec.model if spec.model is not None else str(req_json.get("model", "claude-sonnet-4-5"))
    )
    usage = spec.usage or {"input_tokens": 24, "output_tokens": 12}
    lines: list[str] = []

    def _emit(event_name: str, payload: dict[str, Any]) -> None:
        lines.append(f"event: {event_name}\ndata: {json.dumps(payload)}\n\n")

    _emit(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_mock_stream",
                "type": "message",
                "role": "assistant",
                "model": model_id,
                "usage": {"input_tokens": usage.get("input_tokens", 24), "output_tokens": 0},
            },
        },
    )
    block_idx = 0
    if spec.text or not spec.tool_calls:
        _emit(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": block_idx,
                "content_block": {"type": "text", "text": ""},
            },
        )
        if spec.text:
            _emit(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": block_idx,
                    "delta": {"type": "text_delta", "text": spec.text},
                },
            )
        _emit("content_block_stop", {"type": "content_block_stop", "index": block_idx})
        block_idx += 1

    for idx, tc in enumerate(spec.tool_calls):
        _emit(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": block_idx,
                "content_block": {
                    "type": "tool_use",
                    "id": str(tc.get("id", f"toolu_{idx}")),
                    "name": _resolve_advertised_tool_name(
                        str(tc["name"]), req_json, dialect="anthropic"
                    ),
                },
            },
        )
        _emit(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": block_idx,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(tc.get("arguments", {})),
                },
            },
        )
        _emit("content_block_stop", {"type": "content_block_stop", "index": block_idx})
        block_idx += 1

    stop_reason = spec.finish_reason or ("tool_use" if spec.tool_calls else "end_turn")
    _emit(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": usage.get("output_tokens", 12)},
        },
    )
    _emit("message_stop", {"type": "message_stop"})
    return "".join(lines).encode("utf-8")


class MockLLMServer:
    """In-process OpenAI/Anthropic/Whisper loopback server with scriptable queue & Transport."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._queue: queue.Queue[ScriptedResponse] = queue.Queue()
        self.requests: list[CapturedHTTPRequest] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self._server.daemon_threads = True
        self.port = int(self._server.server_port)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def openai_endpoint(self) -> str:
        return f"{self.base_url}/v1/chat/completions"

    @property
    def anthropic_endpoint(self) -> str:
        return f"{self.base_url}/v1/messages"

    @property
    def stt_endpoint(self) -> str:
        return f"{self.base_url}/v1/audio/transcriptions"

    def queue_response(self, response: ScriptedResponse) -> None:
        self._queue.put(response)

    def queue_text(
        self,
        text: str,
        *,
        usage: dict[str, int] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        model: str | None = None,
        stream: bool | None = None,
    ) -> None:
        self.queue_response(
            ScriptedResponse(
                status_code=200,
                text=text,
                tool_calls=list(tool_calls or []),
                usage=usage,
                model=model,
                stream=stream,
            )
        )

    def queue_tool_calls(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        text: str = "",
        usage: dict[str, int] | None = None,
    ) -> None:
        self.queue_response(
            ScriptedResponse(
                status_code=200,
                text=text,
                tool_calls=list(tool_calls),
                usage=usage,
            )
        )

    def queue_error(
        self,
        status_code: int,
        *,
        body: bytes | None = None,
        raise_connect_error: bool = False,
        raise_timeout: bool = False,
    ) -> None:
        """Queue an HTTP error status or transport fault.

        Over direct HTTP, ``raise_connect_error=True`` simulates an abrupt post-accept
        disconnect (``httpx.RemoteProtocolError``); pre-TCP-handshake ``httpx.ConnectError``
        uses ``unused_loopback_port()`` (or ``as_mock_transport()``). ``raise_timeout=True``
        holds the response until the client timeout expires (or ``MockLLMServer.close()``).
        """
        raw = body if body is not None else json.dumps({"error": {"code": status_code}}).encode()
        self.queue_response(
            ScriptedResponse(
                status_code=status_code,
                raw_body=raw,
                raise_connect_error=raise_connect_error,
                raise_timeout=raise_timeout,
            )
        )

    def queue_malformed(self, raw_body: bytes = b"{not-json", *, status_code: int = 200) -> None:
        self.queue_response(ScriptedResponse(status_code=status_code, raw_body=raw_body))

    def queue_model_mismatch(self, wrong_model: str = "wrong-model-id") -> None:
        self.queue_response(ScriptedResponse(status_code=200, model=wrong_model))

    def clear(self) -> None:
        with self._lock:
            self.requests.clear()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _next_spec(self) -> ScriptedResponse:
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return ScriptedResponse()

    def dispatch(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body_bytes: bytes,
    ) -> tuple[int, dict[str, str], bytes, str | None]:
        parsed_json: dict[str, Any] | None = None
        if body_bytes:
            try:
                loaded = json.loads(body_bytes.decode("utf-8"))
                if isinstance(loaded, dict):
                    parsed_json = loaded
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed_json = None

        normalized_headers = {k.lower(): v for k, v in headers.items()}
        with self._lock:
            self.requests.append(
                CapturedHTTPRequest(
                    method=method.upper(),
                    path=path,
                    headers=normalized_headers,
                    body_bytes=body_bytes,
                    json_body=parsed_json,
                )
            )

        spec = self._next_spec()
        if spec.raise_connect_error:
            return (0, {}, b"", "connect_error")
        if spec.raise_timeout:
            return (0, {}, b"", "timeout")

        if spec.raw_body is not None:
            resp_headers = {"Content-Type": "application/json", **spec.headers}
            return (spec.status_code, resp_headers, spec.raw_body, None)

        if spec.status_code != 200:
            err_payload = json.dumps({"error": {"status": spec.status_code}}).encode("utf-8")
            resp_headers = {"Content-Type": "application/json", **spec.headers}
            return (spec.status_code, resp_headers, err_payload, None)

        req_json = parsed_json or {}
        want_stream = (
            spec.stream if spec.stream is not None else bool(req_json.get("stream", False))
        )

        if path.endswith("/v1/audio/transcriptions"):
            body = json.dumps({"text": spec.text}).encode("utf-8")
            return (200, {"Content-Type": "application/json"}, body, None)

        if path.endswith("/v1/messages"):
            if want_stream:
                body = _encode_anthropic_sse(req_json, spec)
                return (200, {"Content-Type": "text/event-stream"}, body, None)
            body = _encode_anthropic_json(req_json, spec)
            return (200, {"Content-Type": "application/json"}, body, None)

        if want_stream:
            body = _encode_openai_sse(req_json, spec)
            return (200, {"Content-Type": "text/event-stream"}, body, None)
        body = _encode_openai_json(req_json, spec)
        return (200, {"Content-Type": "application/json"}, body, None)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0") or "0")
                body_bytes = self.rfile.read(length) if length > 0 else b""
                path = urlsplit(self.path).path
                status, resp_headers, payload, fault = owner.dispatch(
                    "POST",
                    path,
                    dict(self.headers.items()),
                    body_bytes,
                )
                if fault == "connect_error":
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        self.connection.close()
                    except OSError:
                        pass
                    return
                if fault == "timeout":
                    self.close_connection = True
                    owner._stop_event.wait(timeout=30.0)
                    return
                self.send_response(status)
                for key, val in resp_headers.items():
                    self.send_header(key, val)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                del format, args

        return _Handler

    def as_mock_transport(self) -> httpx.MockTransport:
        """Return an httpx.MockTransport bridging cloud/loopback URLs to this mock server."""

        def _handle(request: httpx.Request) -> httpx.Response:
            status, resp_headers, payload, fault = self.dispatch(
                request.method,
                request.url.path,
                dict(request.headers.items()),
                request.content,
            )
            if fault == "connect_error":
                raise httpx.ConnectError("Simulated connection failure", request=request)
            if fault == "timeout":
                raise httpx.ReadTimeout("Simulated read timeout", request=request)
            return httpx.Response(status_code=status, headers=resp_headers, content=payload)

        return httpx.MockTransport(_handle)

    def close(self) -> None:
        self._stop_event.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)


class FakeSpeechToText:
    """Scripted SpeechToText double draining bounded_microphone frames."""

    def __init__(
        self,
        transcript: str = "Hello MAX",
        error: Exception | None = None,
        *,
        available: bool = True,
        reason: str | None = None,
    ) -> None:
        self.transcript = transcript
        self.error = error
        self.available = available
        self.reason = reason
        self.received_bytes = 0
        self.calls = 0

    async def recognize(self, audio: AsyncIterator[bytes]) -> str:
        self.calls += 1
        async for frame in audio:
            self.received_bytes += len(frame)
        if self.error is not None:
            raise self.error
        return self.transcript


class FakeTextToSpeech:
    """Deterministic TextToSpeech double yielding bounded PCM-like byte chunks."""

    def __init__(
        self,
        *,
        frame_bytes: bytes = b"PCM-CHUNK-",
        chunks_per_sentence: int = 2,
        delay_seconds: float = 0.0,
        oversized_frame: bool = False,
    ) -> None:
        self.frame_bytes = frame_bytes
        self.chunks_per_sentence = chunks_per_sentence
        self.delay_seconds = delay_seconds
        self.oversized_frame = oversized_frame
        self.spoken_sentences: list[str] = []

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        self.spoken_sentences.append(text)
        if self.oversized_frame:
            yield b"X" * (256 * 1024 + 1)
            return
        for idx in range(self.chunks_per_sentence):
            if self.delay_seconds > 0:
                await asyncio.sleep(self.delay_seconds)
            yield self.frame_bytes + f"{idx}:{text[:24]}".encode()


class RecordingAudioSink:
    """AudioSink double tracking played frames and stop() invocations."""

    def __init__(self, on_play: Callable[[int], None] | None = None) -> None:
        self.played_frames: list[bytes] = []
        self.stop_calls = 0
        self.on_play = on_play

    async def play(self, frame: bytes) -> None:
        self.played_frames.append(frame)
        if self.on_play is not None:
            self.on_play(len(self.played_frames))

    async def stop(self) -> None:
        self.stop_calls += 1


class FakeMicrophoneRecorder:
    """Deterministic MicrophoneRecorder double avoiding host QMediaDevices/QAudioSource."""

    def __init__(
        self,
        *,
        chunks: Sequence[bytes] = (b"\x01\x02" * 160,),
        available: bool = True,
        headless: bool = False,
        reason: str | None = None,
        sample_rate: int = 16_000,
        channels: int = 1,
        start_result: bool = True,
    ) -> None:
        self._chunks = list(chunks)
        self._available = available
        self._headless = headless
        self._reason = reason
        self._sample_rate = sample_rate
        self._channels = channels
        self._start_result = start_result
        self.start_calls = 0
        self.drain_calls = 0
        self.discard_calls = 0

    @property
    def available(self) -> bool:
        return self._available

    @property
    def headless(self) -> bool:
        return self._headless

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    def start(self) -> bool:
        self.start_calls += 1
        return self._start_result

    def stop_and_drain(self) -> list[bytes]:
        self.drain_calls += 1
        return list(self._chunks)

    def discard(self) -> None:
        self.discard_calls += 1


class _FakeTtsLocale:
    def __init__(self, bcp47: str) -> None:
        self._bcp47 = bcp47

    def name(self) -> str:
        return self._bcp47.replace("-", "_")

    def bcp47Name(self) -> str:
        return self._bcp47


class _FakeTtsVoice:
    def __init__(self, name: str, bcp47: str, gender: str) -> None:
        self._name = name
        self._locale = _FakeTtsLocale(bcp47)
        self._gender = gender

    def name(self) -> str:
        return self._name

    def locale(self) -> _FakeTtsLocale:
        return self._locale

    def gender(self) -> str:
        return self._gender


class _FakeTtsDriver:
    def __init__(self, engine_name: str = "sapi") -> None:
        self._engine_name = engine_name
        self.spoken: list[str] = []
        self.selected_voice: Any = None
        self.selected_locale: Any = None
        self._voices = [
            _FakeTtsVoice("en-US-male", "en-US", "Male"),
            _FakeTtsVoice("en-GB-female", "en-GB", "Female"),
        ]

    def engine(self) -> str:
        return self._engine_name

    def availableVoices(self) -> list[_FakeTtsVoice]:
        return list(self._voices)

    def allVoices(self, _locale: Any) -> list[_FakeTtsVoice]:
        return list(self._voices)

    def setLocale(self, locale: Any) -> None:
        self.selected_locale = locale

    def setVoice(self, voice: Any) -> None:
        self.selected_voice = voice

    def say(self, text: str) -> None:
        self.spoken.append(text)

    def stop(self, _hint: Any = None) -> None:
        return None

    def state(self) -> str:
        return "Ready"

    def errorReason(self) -> str:
        return "NoError"


class _FakeAudioOutputDevice:
    def isNull(self) -> bool:
        return False


def make_fake_desktop_tts(
    preference: Any | None = None,
    *,
    engine: str = "sapi",
    headless: bool = False,
) -> Any:
    """Create a non-headless DesktopTextToSpeech backed by an in-memory fake Qt driver."""
    from max_core.persona import VoicePreference
    from max_core.voice.desktop_tts import DesktopTextToSpeech

    pref = preference if preference is not None else VoicePreference()
    driver = _FakeTtsDriver(engine_name=engine)
    return DesktopTextToSpeech(
        preference=pref,
        headless=headless,
        engine_factory=lambda _name: driver,
        available_engines_fn=lambda: [engine],
        audio_outputs_fn=lambda: [_FakeAudioOutputDevice()],
    )


def create_mocked_max_window(
    window_cls: type[Any],
    core: NativeDesktopCore,
    server: MockLLMServer,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Instantiate MaxWindow with MockLLMServer transport wired into chat/httpx layers."""
    from max_core.assistant_chat import AssistantChatService

    transport = server.as_mock_transport()
    orig_chat_init = AssistantChatService.__init__

    def _patched_chat_init(self: Any, *args: Any, **kwargs: Any) -> None:
        if (
            kwargs.get("transport") is None
            and kwargs.get("client") is None
            and kwargs.get("client_factory") is None
        ):
            kwargs["transport"] = transport
        orig_chat_init(self, *args, **kwargs)

    orig_async_client_init = httpx.AsyncClient.__init__

    def _patched_async_client_init(self: Any, *args: Any, **kwargs: Any) -> None:
        if kwargs.get("transport") is None:
            kwargs["transport"] = transport
        orig_async_client_init(self, *args, **kwargs)

    monkeypatch.setattr(AssistantChatService, "__init__", _patched_chat_init)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", _patched_async_client_init)

    win_sig = inspect.signature(window_cls.__init__)
    win_kwargs: dict[str, Any] = {}
    if "transport" in win_sig.parameters:
        win_kwargs["transport"] = transport
    elif "chat_transport" in win_sig.parameters:
        win_kwargs["chat_transport"] = transport
    return window_cls(core, **win_kwargs)


async def synthetic_audio_stream(
    frames: Sequence[bytes] = (b"\x01\x02" * 160, b"\x03\x04" * 160),
) -> AsyncIterator[bytes]:
    """Yield synthetic audio frames deterministically."""
    for frame in frames:
        yield frame


@pytest.fixture
def clean_desktop_env(tmp_path: Path) -> dict[str, str]:
    """Isolated environment dictionary rooted inside tmp_path."""
    return build_clean_desktop_env(tmp_path)


@pytest.fixture
def run_desktop_subprocess(
    tmp_path: Path,
    clean_desktop_env: dict[str, str],
) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Run a Python subprocess in an isolated cwd (never repo root with .env)."""

    def _run(
        args: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float = 25.0,
    ) -> subprocess.CompletedProcess[str]:
        effective_cwd = tmp_path if cwd is None else cwd
        effective_env = dict(clean_desktop_env if env is None else env)
        return subprocess.run(
            [sys.executable, *args],
            cwd=effective_cwd,
            env=effective_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    return _run


@pytest.fixture
def desktop_core_factory() -> Iterator[Callable[..., NativeDesktopCore]]:
    """Create NativeDesktopCore instances and close all of them during teardown."""
    created: list[NativeDesktopCore] = []

    def _create(
        *,
        config: DesktopConfig | None = None,
        database_url: str | None = None,
        store: TaskStore | None = None,
    ) -> NativeDesktopCore:
        if config is not None and database_url is None and store is None:
            core = NativeDesktopCore.from_env(config=config)
        else:
            core = NativeDesktopCore(database_url=database_url, store=store, config=config)
        created.append(core)
        return core

    try:
        yield _create
    finally:
        for core in reversed(created):
            core.close()


@pytest.fixture
def mock_llm_server() -> Iterator[MockLLMServer]:
    """Yield a running loopback OpenAI/Anthropic/Whisper mock server and stop it cleanly."""
    server = MockLLMServer()
    try:
        yield server
    finally:
        server.close()
