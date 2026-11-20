from __future__ import annotations

import asyncio
import enum
import importlib
import io
import json
import sys
import types
import wave
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from max_core.local_config import DesktopConfig, ProviderConfig
from max_core.persona import VoicePreference
from max_core.presence.notifications import FocusState
from max_core.presence.orb import OrbEvent, OrbReducer, OrbState, OrbVisibility
from max_core.voice.barge_in import BargeInGate
from max_core.voice.controller import VoiceCapabilities, VoiceController
from max_core.voice.desktop_stt import (
    DEFAULT_STT_ENDPOINT,
    DesktopSpeechToText,
    QtMicrophoneRecorder,
    derive_transcription_endpoint,
    pcm16_to_wav,
)
from max_core.voice.desktop_tts import (
    DesktopAudioSink,
    DesktopTextToSpeech,
    select_qt_voice,
)
from max_core.voice.pipeline import (
    MAX_INPUT_AUDIO_BYTES,
    MAX_INPUT_FRAME_BYTES,
    MAX_OUTPUT_FRAME_BYTES,
    MAX_TRANSCRIPT_CHARS,
    VoiceError,
    VoiceTurnPipeline,
)


class _FakeLocale:
    def __init__(self, bcp47: str, name: str | None = None) -> None:
        self._bcp47 = bcp47
        self._name = name if name is not None else bcp47.replace("-", "_")

    def bcp47Name(self) -> str:
        return self._bcp47

    def name(self) -> str:
        return self._name


class _FakeVoice:
    def __init__(self, name: str, locale_tag: str, gender: str) -> None:
        self._name = name
        self._locale = _FakeLocale(locale_tag)
        self._gender = gender

    def name(self) -> str:
        return self._name

    def locale(self) -> _FakeLocale:
        return self._locale

    def gender(self) -> str:
        return self._gender


class _FakeTtsDriver:
    def __init__(
        self,
        engine: str = "winrt",
        voices: Sequence[_FakeVoice] = (),
        all_voices: Sequence[_FakeVoice] = (),
    ) -> None:
        self._engine = engine
        self._available_voices = list(voices)
        self._all_voices = list(all_voices)
        self.spoken: list[str] = []
        self.stop_calls = 0
        self.selected_voice: _FakeVoice | None = None
        self.selected_locale: _FakeLocale | None = None
        self._state = "ready"
        self.all_voices_queries: list[object] = []

    def engine(self) -> str:
        return self._engine

    def availableVoices(self) -> Sequence[_FakeVoice]:
        return list(self._available_voices)

    def allVoices(self, locale: object) -> Sequence[_FakeVoice]:
        self.all_voices_queries.append(locale)
        return list(self._all_voices)

    def setLocale(self, locale: _FakeLocale) -> None:
        self.selected_locale = locale

    def setVoice(self, voice: _FakeVoice) -> None:
        self.selected_voice = voice

    def say(self, text: str) -> None:
        self._state = "speaking"
        self.spoken.append(text)

    def stop(self, boundaryHint: object = None) -> None:
        self.stop_calls += 1
        self._state = "ready"

    def state(self) -> str:
        return self._state

    def errorReason(self) -> int:
        return 0


class _FakeAudioDevice:
    def __init__(self, *, null: bool = False) -> None:
        self._null = null

    def isNull(self) -> bool:
        return self._null

    def isFormatSupported(self, fmt: Any) -> bool:
        return True

    def preferredFormat(self) -> Any:
        return None


class _FakeRecorder:
    def __init__(
        self,
        *,
        available: bool = True,
        headless: bool = False,
        reason: str | None = None,
        chunks: list[bytes] | None = None,
        start_ok: bool = True,
    ) -> None:
        self._available = available
        self._headless = headless
        self._reason = reason
        self._chunks = list(chunks) if chunks is not None else [b"\x01\x02" * 160]
        self._start_ok = start_ok
        self.started = False
        self.start_calls = 0
        self.discard_calls = 0
        self.drain_calls = 0

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
        return 16_000

    @property
    def channels(self) -> int:
        return 1

    def start(self) -> bool:
        self.start_calls += 1
        if not self._available or not self._start_ok:
            return False
        self.started = True
        return True

    def stop_and_drain(self) -> list[bytes]:
        self.drain_calls += 1
        self.started = False
        drained = list(self._chunks)
        self._chunks.clear()
        return drained

    def discard(self) -> None:
        self.discard_calls += 1
        self.started = False
        self._chunks.clear()


class _FakeGateway:
    def __init__(self, events: list[tuple[str, str]]) -> None:
        self._events = events
        self.requests: list[Any] = []

    async def stream(self, request: Any) -> AsyncIterator[Any]:
        self.requests.append(request)
        for kind, text in self._events:
            yield SimpleNamespace(kind=kind, text=text)


def _make_tts_with_driver(
    driver: _FakeTtsDriver,
    *,
    preference: VoicePreference | None = None,
) -> DesktopTextToSpeech:
    return DesktopTextToSpeech(
        preference=preference,
        headless=False,
        engine_factory=lambda _name: driver,
        available_engines_fn=lambda: ["winrt", "sapi"],
        audio_outputs_fn=lambda: [_FakeAudioDevice()],
    )


def test_voice_preference_selection_prefers_feminine_en_gb() -> None:
    voices = [
        _FakeVoice("Bob", "en-GB", "Male"),
        _FakeVoice("Microsoft Zira", "en-US", "Female"),
        _FakeVoice("Hazel", "en-GB", "Female"),
        _FakeVoice("Hans", "de-DE", "Male"),
    ]
    chosen = select_qt_voice(voices, VoicePreference(presentation="feminine", locale="en-GB"))
    assert chosen is not None
    assert chosen.name() == "Hazel"

    fallback_female = select_qt_voice(
        [
            _FakeVoice("Microsoft David", "en-US", "Male"),
            _FakeVoice("Microsoft Zira", "en-US", "Female"),
            _FakeVoice("George", "en-GB", "Male"),
        ],
        VoicePreference(presentation="feminine", locale="en-GB"),
    )
    assert fallback_female is not None
    assert fallback_female.name() == "Microsoft Zira"


def test_desktop_tts_uses_driver_and_stops_immediately() -> None:
    driver = _FakeTtsDriver(
        engine="winrt",
        voices=[
            _FakeVoice("Microsoft David", "en-US", "Male"),
            _FakeVoice("Microsoft Zira", "en-US", "Female"),
        ],
    )
    tts = _make_tts_with_driver(driver)
    assert tts.available is True
    assert tts.engine_name == "winrt"
    assert tts.reason is None
    assert tts.selected_voice_name == "Microsoft Zira"
    assert driver.selected_voice is not None
    assert driver.selected_voice.name() == "Microsoft Zira"

    assert tts.speak("  Good morning.  ") is True
    assert driver.spoken == ["Good morning."]

    tts.stop()
    assert driver.stop_calls == 1
    assert tts.is_speaking() is False


def test_desktop_tts_chunks_large_utf8_and_sink_validates_frames() -> None:
    driver = _FakeTtsDriver()
    tts = _make_tts_with_driver(driver)
    sink = DesktopAudioSink(tts)

    async def check() -> None:
        long_text = "é" * 150_000  # 300,000 UTF-8 bytes > 256 KB
        frames = [frame async for frame in tts.stream(long_text)]
        assert len(frames) == 2
        assert all(0 < len(frame) <= MAX_OUTPUT_FRAME_BYTES for frame in frames)
        assert b"".join(frames).decode("utf-8") == long_text

        with pytest.raises(VoiceError, match="invalid_speech_frame"):
            await sink.play(b"")
        with pytest.raises(VoiceError, match="invalid_speech_frame"):
            await sink.play(b"x" * (MAX_OUTPUT_FRAME_BYTES + 1))

        await sink.play(b"Hello")
        assert driver.spoken == ["Hello"]
        await sink.stop()
        assert driver.stop_calls == 1
        await sink.play(b"Ignored after stop")
        assert driver.spoken == ["Hello"]

    asyncio.run(check())


def test_voice_turn_pipeline_end_to_end_with_desktop_adapters() -> None:
    driver = _FakeTtsDriver()
    tts = _make_tts_with_driver(driver)
    sink = DesktopAudioSink(tts)
    captured_wav: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_wav.append(request.content)
        return httpx.Response(200, json={"text": "What is my status?"})

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            stt = DesktopSpeechToText(
                api_key="sk-test-1234",
                http_client=client,
            )
            gateway = _FakeGateway(
                [
                    ("start", ""),
                    ("content_delta", "Systems are nominal. "),
                    ("content_delta", "Ready when you are."),
                    ("tool_proposal", "ignored_tool_call"),
                    ("complete", ""),
                ]
            )

            async def audio() -> AsyncIterator[bytes]:
                yield b"\x00\x01" * 320

            result = await VoiceTurnPipeline(gateway).run(
                audio(),
                stt,
                lambda text: f"trusted:{text}",
                tts,
                sink,
                asyncio.Event(),
            )
            assert result.interrupted is False
            assert result.speech_chunks == 2
            assert result.audio_frames == 2
            assert gateway.requests == ["trusted:What is my status?"]
            assert driver.spoken == ["Systems are nominal.", "Ready when you are."]
            assert len(captured_wav) == 1

    asyncio.run(check())


def test_pcm16_to_wav_produces_valid_wave_bytes() -> None:
    pcm = b"\x01\x00\xff\x7f" * 100
    wav_bytes = pcm16_to_wav(pcm, sample_rate=16_000, channels=1)
    assert wav_bytes[:4] == b"RIFF"
    assert wav_bytes[8:12] == b"WAVE"

    import io

    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 16_000
        assert wf.readframes(200) == pcm


def test_openai_stt_endpoint_derivation_and_mock_transport() -> None:
    provider = ProviderConfig(
        provider="openai",
        model_id="gpt-4o-mini",
        endpoint="https://api.openai.com/v1/chat/completions",
        api_key="sk-secret-value-999",
    )
    assert (
        derive_transcription_endpoint(provider.endpoint)
        == "https://api.openai.com/v1/audio/transcriptions"
    )
    assert (
        derive_transcription_endpoint("http://127.0.0.1:8080/v1")
        == "http://127.0.0.1:8080/v1/audio/transcriptions"
    )

    seen_requests: list[tuple[str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(
            (
                str(request.url),
                request.headers.get("Authorization", ""),
                request.content,
            )
        )
        return httpx.Response(200, json={"text": "  Check my tasks  "})

    async def audio() -> AsyncIterator[bytes]:
        yield b"\x02\x00" * 160

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            stt = DesktopSpeechToText(provider_config=provider, http_client=client)
            assert stt.available is True
            assert stt.endpoint == DEFAULT_STT_ENDPOINT
            assert stt.model == "whisper-1"
            assert "sk-secret-value-999" not in repr(stt)

            text = await stt.recognize(audio())
            assert text == "Check my tasks"

    asyncio.run(check())
    assert len(seen_requests) == 1
    url, auth_header, body = seen_requests[0]
    assert url == "https://api.openai.com/v1/audio/transcriptions"
    assert auth_header == "Bearer sk-secret-value-999"
    assert b'name="model"\r\n\r\nwhisper-1' in body
    assert b"gpt-4o-mini" not in body
    assert b"RIFF" in body and b"WAVE" in body


def test_desktop_config_uses_openai_even_when_anthropic_active() -> None:
    anthropic_cfg = ProviderConfig(
        provider="anthropic",
        model_id="claude-sonnet-4-5",
        endpoint="https://api.anthropic.com/v1/messages",
        api_key="sk-ant-secret",
    )
    openai_cfg = ProviderConfig(
        provider="openai",
        model_id="gpt-4o-mini",
        endpoint="https://api.openai.com/v1/chat/completions",
        api_key="sk-openai-secret",
    )
    desktop_cfg = DesktopConfig(
        database_url="sqlite+pysqlite:///unused.sqlite3",
        is_sqlite_local=True,
        workspace_root=Path("."),
        notes_dir=Path("."),
        active_provider=anthropic_cfg,
        openai=openai_cfg,
        anthropic=anthropic_cfg,
    )
    stt = DesktopSpeechToText(desktop_config=desktop_cfg, model="gpt-4o-mini-transcribe")
    assert stt.available is True
    assert stt.model == "gpt-4o-mini-transcribe"
    assert stt.endpoint == "https://api.openai.com/v1/audio/transcriptions"
    assert "sk-openai-secret" not in repr(stt)
    assert "sk-ant-secret" not in repr(stt)


@pytest.mark.parametrize(
    ("status_code", "payload", "expected_code"),
    [
        (401, {"error": {"message": "bad key sk-leak"}}, "stt_auth_failed"),
        (403, {"error": "forbidden"}, "stt_auth_failed"),
        (429, {"error": "slow down"}, "stt_rate_limited"),
        (400, {"error": "bad audio"}, "stt_invalid_request"),
        (502, {"error": "upstream down"}, "stt_provider_unavailable"),
        (302, {"location": "https://evil.example"}, "stt_redirect_denied"),
    ],
)
def test_openai_stt_http_errors_are_sanitized(
    status_code: int,
    payload: dict[str, Any],
    expected_code: str,
) -> None:
    secret_key = "sk-super-secret-xyz"

    def handler(_request: httpx.Request) -> httpx.Response:
        headers = {"Location": "https://evil.example"} if status_code == 302 else {}
        return httpx.Response(status_code, json=payload, headers=headers)

    async def audio() -> AsyncIterator[bytes]:
        yield b"\x01\x02" * 64

    async def check() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            stt = DesktopSpeechToText(api_key=secret_key, http_client=client)
            with pytest.raises(VoiceError) as exc_info:
                await stt.recognize(audio())
            assert str(exc_info.value) == expected_code
            assert secret_key not in str(exc_info.value)
            assert "evil.example" not in str(exc_info.value)

    asyncio.run(check())


def test_openai_stt_network_timeout_oversize_and_missing_key() -> None:
    stt_missing = DesktopSpeechToText()
    assert stt_missing.available is False
    assert stt_missing.reason == "stt_not_configured"

    async def audio() -> AsyncIterator[bytes]:
        yield b"\x01\x02" * 64

    async def check_missing() -> None:
        with pytest.raises(VoiceError, match="stt_not_configured"):
            await stt_missing.recognize(audio())

    asyncio.run(check_missing())

    with pytest.raises(VoiceError, match="invalid_api_key"):
        DesktopSpeechToText(api_key="sk-bad\r\nkey")

    with pytest.raises(VoiceError, match="invalid_stt_endpoint"):
        DesktopSpeechToText(
            api_key="sk-ok", endpoint="http://api.openai.com/v1/audio/transcriptions"
        )

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    def net_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection failed", request=request)

    def oversize_handler(_request: httpx.Request) -> httpx.Response:
        big = json.dumps({"text": "a" * 1_100_000}).encode("utf-8")
        return httpx.Response(200, content=big)

    async def check_errors() -> None:
        for fn, code in (
            (timeout_handler, "stt_timeout"),
            (net_handler, "stt_network_error"),
            (oversize_handler, "stt_response_too_large"),
        ):
            async with httpx.AsyncClient(transport=httpx.MockTransport(fn)) as client:
                stt = DesktopSpeechToText(api_key="sk-secret", http_client=client)
                with pytest.raises(VoiceError) as exc_info:
                    await stt.recognize(audio())
                assert str(exc_info.value) == code
                assert "sk-secret" not in str(exc_info.value)

    asyncio.run(check_errors())


def test_stt_rejects_oversized_audio_and_transcript() -> None:
    stt = DesktopSpeechToText(transcribe_fn=lambda _raw: "ok")

    async def too_big_frame() -> AsyncIterator[bytes]:
        yield b"x" * (MAX_INPUT_FRAME_BYTES + 1)

    async def too_big_total() -> AsyncIterator[bytes]:
        chunk = b"x" * MAX_INPUT_FRAME_BYTES
        for _ in range((MAX_INPUT_AUDIO_BYTES // MAX_INPUT_FRAME_BYTES) + 1):
            yield chunk

    stt_huge_text = DesktopSpeechToText(transcribe_fn=lambda _raw: "x" * (MAX_TRANSCRIPT_CHARS + 1))

    async def small_audio() -> AsyncIterator[bytes]:
        yield b"\x01\x02" * 32

    async def check() -> None:
        with pytest.raises(VoiceError, match="invalid_audio_frame"):
            await stt.recognize(too_big_frame())
        with pytest.raises(VoiceError, match="audio_limit_exceeded"):
            await stt.recognize(too_big_total())
        with pytest.raises(VoiceError, match="transcript_too_large"):
            await stt_huge_text.recognize(small_audio())

    asyncio.run(check())


def test_voice_controller_push_to_talk_lifecycle_orb_and_untrusted_text() -> None:
    now = 1_000

    def clock() -> int:
        return now

    driver = _FakeTtsDriver()
    tts = _make_tts_with_driver(driver)
    recorder = _FakeRecorder(chunks=[b"\x10\x20" * 160])
    gateway = _FakeGateway([("content_delta", "Noted."), ("complete", "")])
    stt = DesktopSpeechToText(
        transcribe_fn=lambda _raw: '{"tool": "workspace.read_file", "path": "secrets"}'
    )
    controller = VoiceController(
        tts=tts,
        stt=stt,
        recorder=recorder,
        gateway=gateway,
        clock_ms=clock,
        focus_probe=lambda: FocusState.ACCEPTS_NOTIFICATIONS,
    )

    # Never auto-records on startup.
    assert recorder.start_calls == 0
    assert controller.is_recording is False
    caps = controller.capabilities()
    assert caps == VoiceCapabilities(
        tts_available=True,
        tts_engine="winrt",
        stt_available=True,
        mic_available=True,
        headless=False,
        reason=None,
    )

    # Start push-to-talk with direct user gesture.
    assert controller.start_push_to_talk(direct_user_gesture=True) is True
    assert controller.is_recording is True
    assert recorder.start_calls == 1
    assert controller.orb_view() == SimpleNamespace(
        state=OrbState.LISTENING, visibility=OrbVisibility.ACTIVE
    ) or (
        controller.orb_view().state == OrbState.LISTENING
        and controller.orb_view().visibility == OrbVisibility.ACTIVE
    )

    now += 100
    transcript = controller.stop_push_to_talk_and_transcribe()
    # Transcript returned purely as untrusted text; gateway/tools were never called.
    assert transcript == '{"tool": "workspace.read_file", "path": "secrets"}'
    assert gateway.requests == []
    assert controller.is_recording is False
    assert recorder.drain_calls == 1
    assert controller.orb_view().state == OrbState.IDLE


def test_voice_controller_activation_gate_denials_and_expiry() -> None:
    now = 5_000

    def clock() -> int:
        return now

    driver = _FakeTtsDriver()
    tts = _make_tts_with_driver(driver)
    recorder = _FakeRecorder(chunks=[b"\x01\x02" * 64])
    stt = DesktopSpeechToText(transcribe_fn=lambda _raw: "hello")

    # 1. Non-user gesture denied
    ctrl = VoiceController(tts=tts, stt=stt, recorder=recorder, clock_ms=clock)
    assert ctrl.start_push_to_talk(direct_user_gesture=False) is False
    assert recorder.start_calls == 0

    # 2. owner_opted_in=False denied
    ctrl_optout = VoiceController(
        tts=tts, stt=stt, recorder=recorder, clock_ms=clock, owner_opted_in=False
    )
    assert ctrl_optout.start_push_to_talk(direct_user_gesture=True) is False
    assert recorder.start_calls == 0

    # 3. os_microphone_permission=False denied
    ctrl_noperm = VoiceController(
        tts=tts, stt=stt, recorder=recorder, clock_ms=clock, os_microphone_permission=False
    )
    assert ctrl_noperm.start_push_to_talk(direct_user_gesture=True) is False

    # 4. foreground=False denied
    ctrl_bg = VoiceController(tts=tts, stt=stt, recorder=recorder, clock_ms=clock, foreground=False)
    assert ctrl_bg.start_push_to_talk(direct_user_gesture=True) is False

    # 5. Active window expiry discards audio cleanly
    assert ctrl.start_push_to_talk(direct_user_gesture=True) is True
    now += 20_000  # > 15_000ms default active window
    assert ctrl.stop_push_to_talk_and_transcribe() is None
    assert ctrl.last_error == "activation_expired_or_revoked"

    # 6. Backgrounding mid-recording discards audio immediately
    recorder2 = _FakeRecorder(chunks=[b"\x01\x02" * 64])
    ctrl2 = VoiceController(tts=tts, stt=stt, recorder=recorder2, clock_ms=clock)
    assert ctrl2.start_push_to_talk(direct_user_gesture=True) is True
    ctrl2.set_presence_flags(foreground=False)
    assert ctrl2.is_recording is False
    assert recorder2.discard_calls == 1
    assert ctrl2.stop_push_to_talk_and_transcribe() is None


def test_voice_controller_barge_in_interrupts_speech() -> None:
    now = 1_000
    driver = _FakeTtsDriver()
    tts = _make_tts_with_driver(driver)
    recorder = _FakeRecorder()
    stt = DesktopSpeechToText(transcribe_fn=lambda _raw: "ignored")
    ctrl = VoiceController(
        tts=tts,
        stt=stt,
        recorder=recorder,
        barge_in_gate=BargeInGate(minimum_voice_frames=3, threshold=0.7),
        clock_ms=lambda: now,
        focus_probe=lambda: FocusState.ACCEPTS_NOTIFICATIONS,
    )

    assert ctrl.speak_text("Speaking a long response.") is True
    assert ctrl.is_speaking is True
    assert ctrl.orb_view().state == OrbState.SPEAKING

    # Echo suppression NOT verified -> must not interrupt.
    for _ in range(4):
        assert ctrl.observe_barge_in(0.95, echo_suppression_verified=False) is False
    assert ctrl.is_speaking is True

    # Two verified frames -> not yet triggered (needs 3).
    assert ctrl.observe_barge_in(0.85, echo_suppression_verified=True) is False
    assert ctrl.observe_barge_in(0.85, echo_suppression_verified=True) is False
    assert ctrl.is_speaking is True

    # Third verified frame -> interrupts TTS immediately.
    assert ctrl.observe_barge_in(0.85, echo_suppression_verified=True) is True
    assert ctrl.is_speaking is False
    assert driver.stop_calls >= 1
    assert ctrl.orb_view().state == OrbState.IDLE


def test_voice_controller_run_voice_turn_and_focus_visibility() -> None:
    now = 2_000
    driver = _FakeTtsDriver()
    tts = _make_tts_with_driver(driver)
    sink = DesktopAudioSink(tts)
    stt = DesktopSpeechToText(transcribe_fn=lambda _raw: "Hello MAX")
    gateway = _FakeGateway([("content_delta", "Hello there."), ("complete", "")])
    focus = FocusState.ACCEPTS_NOTIFICATIONS

    ctrl = VoiceController(
        tts=tts,
        sink=sink,
        stt=stt,
        recorder=_FakeRecorder(),
        gateway=gateway,
        clock_ms=lambda: now,
        focus_probe=lambda: focus,
    )

    async def audio() -> AsyncIterator[bytes]:
        yield b"\x01\x02" * 64

    result = asyncio.run(ctrl.run_voice_turn(audio(), lambda text: f"req:{text}"))
    assert result.interrupted is False
    assert result.speech_chunks == 1
    assert driver.spoken == ["Hello there."]
    assert ctrl.orb_view().state == OrbState.IDLE

    focus = FocusState.GAME_OR_FULLSCREEN
    assert ctrl.orb_view().visibility == OrbVisibility.PASSIVE
    assert ctrl.orb_view(quiet_mode=True).visibility == OrbVisibility.PASSIVE


def test_headless_offscreen_and_missing_pyside6_degrade_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    tts = DesktopTextToSpeech()
    recorder = QtMicrophoneRecorder()
    stt = DesktopSpeechToText()
    ctrl = VoiceController(tts=tts, recorder=recorder, stt=stt)

    caps = ctrl.capabilities()
    assert caps.headless is True
    assert caps.tts_available is False
    assert caps.tts_engine == "mock"
    assert caps.mic_available is False
    assert caps.stt_available is False
    assert caps.reason == "headless_offscreen"

    assert ctrl.speak_text("Hello") is False
    assert ctrl.start_push_to_talk(direct_user_gesture=True) is False
    assert ctrl.stop_push_to_talk_and_transcribe() is None
    ctrl.interrupt_speech()

    # Pipeline still runs cleanly with silent/headless TTS + sink when STT is injected.
    async def check_headless_pipeline() -> None:
        headless_stt = DesktopSpeechToText(transcribe_fn=lambda _raw: "Ping")
        gateway = _FakeGateway([("content_delta", "Pong."), ("complete", "")])

        async def audio() -> AsyncIterator[bytes]:
            yield b"\x01\x02" * 32

        result = await VoiceTurnPipeline(gateway).run(
            audio(),
            headless_stt,
            lambda text: text,
            tts,
            DesktopAudioSink(tts),
            asyncio.Event(),
        )
        assert result.interrupted is False
        assert result.speech_chunks == 1
        assert result.audio_frames == 1

    asyncio.run(check_headless_pipeline())

    # Simulate PySide6 absent in non-headless environment.
    monkeypatch.setenv("QT_QPA_PLATFORM", "")
    real_import_module = importlib.import_module

    def fake_import(name: str, package: str | None = None) -> types.ModuleType:
        if name.startswith("PySide6"):
            raise ModuleNotFoundError(name)
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    no_qt_tts = DesktopTextToSpeech(headless=False)
    no_qt_rec = QtMicrophoneRecorder(headless=False)
    assert no_qt_tts.available is False
    assert no_qt_tts.engine_name == "none"
    assert no_qt_tts.reason == "pyside6_unavailable"
    assert no_qt_rec.available is False
    assert no_qt_rec.reason == "pyside6_unavailable"


def test_pyside6_zero_audio_devices_and_null_device_degrade_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("PySide6")
    from PySide6 import QtCore, QtMultimedia, QtTextToSpeech

    monkeypatch.setenv("QT_QPA_PLATFORM", "")
    monkeypatch.setattr(QtMultimedia.QMediaDevices, "audioOutputs", lambda: [])
    monkeypatch.setattr(QtMultimedia.QMediaDevices, "audioInputs", lambda: [])
    monkeypatch.setattr(
        QtMultimedia.QMediaDevices,
        "defaultAudioInput",
        lambda: QtMultimedia.QAudioDevice(),
    )

    def fail_if_constructed(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("QAudioSource must not be constructed when no audio input exists")

    monkeypatch.setattr(QtMultimedia, "QAudioSource", fail_if_constructed)

    tts = DesktopTextToSpeech(headless=False)
    recorder = QtMicrophoneRecorder(headless=False)

    assert tts.available is False
    assert tts.engine_name == "mock"
    assert tts.reason == "no_audio_outputs"

    assert recorder.available is False
    assert recorder.reason == "no_audio_input"
    assert recorder.start() is False
    assert recorder.stop_and_drain() == []

    # Verify QLocale.c() (not QLocale.Language) is passed to allVoices() fallback.
    driver = _FakeTtsDriver(
        engine="sapi",
        voices=[],
        all_voices=[_FakeVoice("Hazel", "en-GB", "Female")],
    )
    monkeypatch.setattr(
        QtMultimedia.QMediaDevices,
        "audioOutputs",
        lambda: [_FakeAudioDevice()],
    )
    monkeypatch.setattr(QtTextToSpeech.QTextToSpeech, "availableEngines", lambda: ["sapi"])
    monkeypatch.setattr(QtTextToSpeech, "QTextToSpeech", lambda *_args: driver)

    tts_sapi = DesktopTextToSpeech(headless=False)
    assert tts_sapi.available is True
    assert tts_sapi.engine_name == "sapi"
    assert tts_sapi.selected_voice_name == "Hazel"
    assert len(driver.all_voices_queries) == 1
    assert isinstance(driver.all_voices_queries[0], QtCore.QLocale)
    assert not isinstance(driver.all_voices_queries[0], QtCore.QLocale.Language)

    # Guard against PySide6 winrt teardown only if anything else touched Qt on Windows.
    assert sys.version_info >= (3, 11)


def test_qt_microphone_recorder_capture_chunking_and_buffer_wiping() -> None:
    pytest.importorskip("PySide6")

    class _MemByteArray:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload

        def data(self) -> bytes:
            return self._payload

    class _FakeQBuffer:
        def __init__(self, initial: bytes) -> None:
            self.payload = initial
            self.opened = False
            self.closed = False
            self.cleared = False

        def open(self, _mode: object) -> bool:
            self.opened = True
            return True

        def close(self) -> None:
            self.closed = True
            self.opened = False

        def data(self) -> _MemByteArray:
            return _MemByteArray(self.payload)

        def setData(self, data: object) -> None:
            self.payload = b""
            self.cleared = True

    class _FakeAudioSource:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self.stopped = False

        def setBufferSize(self, _size: int) -> None:
            pass

        def start(self, buf: _FakeQBuffer) -> None:
            buf.payload = self._payload

        def stop(self) -> None:
            self.stopped = True

        def error(self) -> int:
            return 0

    payload_300k = b"\x01\x02" * 150_000  # 300,000 bytes -> 2 frames
    buf_holder: list[_FakeQBuffer] = []

    def make_buf() -> _FakeQBuffer:
        buf = _FakeQBuffer(b"")
        buf_holder.append(buf)
        return buf

    recorder = QtMicrophoneRecorder(
        headless=False,
        audio_inputs_fn=lambda: [_FakeAudioDevice()],
        default_input_fn=lambda: _FakeAudioDevice(),
        source_factory=lambda _dev, _fmt: _FakeAudioSource(payload_300k),
        buffer_factory=make_buf,
    )
    assert recorder.available is True
    assert recorder.recording is False
    assert recorder.start() is True
    assert recorder.recording is True
    assert recorder.start() is False  # Already recording

    frames = recorder.stop_and_drain()
    assert recorder.recording is False
    assert len(frames) == 2
    assert all(0 < len(frame) <= MAX_INPUT_FRAME_BYTES for frame in frames)
    assert b"".join(frames) == payload_300k
    assert buf_holder[0].closed is True
    assert buf_holder[0].cleared is True
    assert buf_holder[0].payload == b""

    # discard() wipes active buffer immediately.
    assert recorder.start() is True
    recorder.discard()
    assert recorder.recording is False
    assert buf_holder[1].cleared is True
    assert recorder.stop_and_drain() == []

    # Oversized > 8 MB raises audio_limit_exceeded and still wipes buffer.
    oversized = b"\x01" * (MAX_INPUT_AUDIO_BYTES + 1)
    rec_big = QtMicrophoneRecorder(
        headless=False,
        audio_inputs_fn=lambda: [_FakeAudioDevice()],
        default_input_fn=lambda: _FakeAudioDevice(),
        source_factory=lambda _dev, _fmt: _FakeAudioSource(oversized),
        buffer_factory=make_buf,
    )
    assert rec_big.start() is True
    with pytest.raises(VoiceError, match="audio_limit_exceeded"):
        rec_big.stop_and_drain()
    assert buf_holder[-1].cleared is True


def test_stt_transport_wav_passthrough_and_async_controller() -> None:
    wav_in = pcm16_to_wav(b"\x01\x02" * 80)
    sent_bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_bodies.append(request.content)
        return httpx.Response(200, json={"text": "Direct WAV"})

    async def wav_audio() -> AsyncIterator[bytes]:
        yield wav_in

    async def check() -> None:
        stt = DesktopSpeechToText(
            api_key="sk-transport-test",
            transport=httpx.MockTransport(handler),
        )
        assert await stt.recognize(wav_audio()) == "Direct WAV"
        assert sent_bodies[0].count(b"RIFF") == 1

        bad_stt = DesktopSpeechToText(
            api_key="sk-transport-test",
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"not-json")),
        )
        with pytest.raises(VoiceError, match="stt_invalid_response"):
            await bad_stt.recognize(wav_audio())

        driver = _FakeTtsDriver()
        tts = _make_tts_with_driver(driver)
        rec = _FakeRecorder(chunks=[b"\x01\x02" * 64])
        ctrl = VoiceController(tts=tts, stt=stt, recorder=rec)
        assert ctrl.start_push_to_talk(direct_user_gesture=True) is True
        assert await ctrl.stop_push_to_talk_and_transcribe_async() == "Direct WAV"

    asyncio.run(check())


def test_desktop_tts_degrades_on_probe_exceptions_and_error_state_when_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom_engines() -> Sequence[str]:
        raise RuntimeError("boom")

    tts_engines_err = DesktopTextToSpeech(
        headless=False,
        audio_outputs_fn=lambda: [_FakeAudioDevice()],
        available_engines_fn=boom_engines,
        engine_factory=lambda _name: _FakeTtsDriver(),
    )
    assert tts_engines_err.available is False
    assert tts_engines_err.engine_name == "mock"
    assert tts_engines_err.reason == "tts_init_failed"

    class _RaisingStateDriver(_FakeTtsDriver):
        def state(self) -> str:
            raise RuntimeError("com_error")

    tts_state_exc = DesktopTextToSpeech(
        headless=False,
        audio_outputs_fn=lambda: [_FakeAudioDevice()],
        available_engines_fn=lambda: ["winrt"],
        engine_factory=lambda _name: _RaisingStateDriver(),
    )
    assert tts_state_exc.available is False
    assert tts_state_exc.reason == "tts_init_failed"

    qt_tts = pytest.importorskip("PySide6.QtTextToSpeech")

    class _QtErrorDriver(_FakeTtsDriver):
        def state(self) -> Any:
            return qt_tts.QTextToSpeech.State.Error

        def errorReason(self) -> Any:
            return qt_tts.QTextToSpeech.ErrorReason.Initialization

    tts_qt_err = DesktopTextToSpeech(
        headless=False,
        audio_outputs_fn=lambda: [_FakeAudioDevice()],
        available_engines_fn=lambda: ["winrt"],
        engine_factory=lambda _name: _QtErrorDriver(),
    )
    assert tts_qt_err.available is False
    assert tts_qt_err.reason == "tts_init_failed"

    # Also verify enum .name fallback when PySide6 cannot be imported.
    class _MockState(enum.Enum):
        Error = 3

    class _MockErr(enum.Enum):
        Initialization = 1

    class _MockEnumErrorDriver(_FakeTtsDriver):
        def state(self) -> Any:
            return _MockState.Error

        def errorReason(self) -> Any:
            return _MockErr.Initialization

    real_import = importlib.import_module

    def no_qt_import(name: str, package: str | None = None) -> types.ModuleType:
        if name.startswith("PySide6"):
            raise ModuleNotFoundError(name)
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", no_qt_import)
    tts_mock_enum_err = DesktopTextToSpeech(
        headless=False,
        audio_outputs_fn=lambda: [_FakeAudioDevice()],
        available_engines_fn=lambda: ["winrt"],
        engine_factory=lambda _name: _MockEnumErrorDriver(),
    )
    assert tts_mock_enum_err.available is False
    assert tts_mock_enum_err.reason == "tts_init_failed"


@pytest.mark.parametrize(
    "bad_timeout",
    [float("nan"), float("inf"), float("-inf"), True, False, 0, -1.5, 120.1],
)
def test_stt_rejects_non_finite_bool_and_out_of_range_timeout(bad_timeout: Any) -> None:
    with pytest.raises(VoiceError, match="invalid_stt_timeout"):
        DesktopSpeechToText(api_key="sk-ok", timeout_seconds=bad_timeout)


@pytest.mark.parametrize(
    "bad_endpoint",
    [
        "https://api.openai.com:99999/v1/audio/transcriptions",
        "https://api.openai.com:abc/v1/audio/transcriptions",
        "https://api.openai.com:0/v1/audio/transcriptions",
    ],
)
def test_stt_rejects_invalid_url_ports(bad_endpoint: str) -> None:
    with pytest.raises(VoiceError, match="invalid_stt_endpoint"):
        derive_transcription_endpoint(bad_endpoint)
    with pytest.raises(VoiceError, match="invalid_stt_endpoint"):
        DesktopSpeechToText(api_key="sk-ok", endpoint=bad_endpoint)


def test_qt_microphone_recorder_open_failure_wipes_local_source_and_buffer() -> None:
    pytest.importorskip("PySide6")

    class _FailOpenBuffer:
        def __init__(self) -> None:
            self.closed = False
            self.cleared = False

        def open(self, _mode: object) -> bool:
            return False

        def close(self) -> None:
            self.closed = True

        def data(self) -> Any:
            return SimpleNamespace(data=lambda: b"")

        def setData(self, _data: object) -> None:
            self.cleared = True

    class _TrackedSource:
        def __init__(self) -> None:
            self.stopped = False

        def setBufferSize(self, _size: int) -> None:
            pass

        def start(self, _buf: Any) -> None:
            raise AssertionError("start must not run when buf.open fails")

        def stop(self) -> None:
            self.stopped = True

        def error(self) -> int:
            return 0

    src = _TrackedSource()
    buf = _FailOpenBuffer()
    rec = QtMicrophoneRecorder(
        headless=False,
        audio_inputs_fn=lambda: [_FakeAudioDevice()],
        default_input_fn=lambda: _FakeAudioDevice(),
        source_factory=lambda _d, _f: src,
        buffer_factory=lambda: buf,
    )
    assert rec.start() is False
    assert rec.recording is False
    assert src.stopped is True
    assert buf.closed is True
    assert buf.cleared is True


def test_voice_controller_is_speaking_resets_on_driver_ready_and_orb_turn_phases() -> None:
    now = 1_000
    driver = _FakeTtsDriver()
    tts = _make_tts_with_driver(driver)
    recorder = _FakeRecorder()

    observed_events: list[OrbState] = []

    class _SpyOrb(OrbReducer):
        def receive(self, event: OrbEvent, *, authenticated: bool, now_ms: int) -> None:
            observed_events.append(event.state)
            super().receive(event, authenticated=authenticated, now_ms=now_ms)

    spy_orb = _SpyOrb(device_id="desktop")
    states_during_stt: list[OrbState] = []

    async def check() -> None:
        def stt_fn(_raw: bytes) -> str:
            states_during_stt.append(ctrl.orb_view().state)
            return "Hello MAX"

        stt = DesktopSpeechToText(transcribe_fn=stt_fn)
        gateway = _FakeGateway([("content_delta", "Good morning."), ("complete", "")])
        ctrl = VoiceController(
            tts=tts,
            stt=stt,
            recorder=recorder,
            gateway=gateway,
            orb=spy_orb,
            clock_ms=lambda: now,
            focus_probe=lambda: FocusState.ACCEPTS_NOTIFICATIONS,
        )

        # Direct speak_text() naturally clears is_speaking once driver reaches Ready.
        assert ctrl.speak_text("Checking voice.") is True
        assert ctrl.is_speaking is True
        driver._state = "ready"
        assert ctrl.is_speaking is False

        observed_events.clear()

        async def audio() -> AsyncIterator[bytes]:
            yield b"\x01\x02" * 32

        result = await ctrl.run_voice_turn(audio(), lambda text: f"req:{text}")
        assert result.speech_chunks == 1
        assert states_during_stt == [OrbState.THINKING]
        assert observed_events == [OrbState.THINKING, OrbState.SPEAKING, OrbState.IDLE]
        # While fake driver remains in "speaking", is_speaking reflects driver state;
        # once driver finishes ("ready"), is_speaking clears cleanly without interrupt_speech().
        assert ctrl.is_speaking is True
        driver._state = "ready"
        assert ctrl.is_speaking is False

    asyncio.run(check())


def test_voice_controller_invalid_clock_and_recorder_format_propagation() -> None:
    driver = _FakeTtsDriver()
    tts = _make_tts_with_driver(driver)

    class _AltFormatRecorder(_FakeRecorder):
        @property
        def sample_rate(self) -> int:
            return 48_000

        @property
        def channels(self) -> int:
            return 2

    captured_payloads: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(request.content)
        return httpx.Response(200, json={"text": "Alternate format"})

    # Invalid clock fails closed without starting the recorder.
    rec_bad_clock = _AltFormatRecorder()
    stt_ok = DesktopSpeechToText(transcribe_fn=lambda _raw: "ok")
    ctrl_bad_clock = VoiceController(
        tts=tts,
        stt=stt_ok,
        recorder=rec_bad_clock,
        clock_ms=lambda: -1,
    )
    assert ctrl_bad_clock.start_push_to_talk(direct_user_gesture=True) is False
    assert ctrl_bad_clock.last_error == "invalid_monotonic_clock"
    assert rec_bad_clock.start_calls == 0

    # Alternate sample_rate/channels are encoded into the WAV sent to STT.
    pcm_48k_stereo = b"\x01\x00\x02\x00" * 100
    rec_alt = _AltFormatRecorder(chunks=[pcm_48k_stereo])
    stt_http = DesktopSpeechToText(
        api_key="sk-alt-format",
        transport=httpx.MockTransport(handler),
    )
    ctrl_alt = VoiceController(
        tts=tts,
        stt=stt_http,
        recorder=rec_alt,
        clock_ms=lambda: 10_000,
    )
    assert ctrl_alt.start_push_to_talk(direct_user_gesture=True) is True
    assert ctrl_alt.stop_push_to_talk_and_transcribe() == "Alternate format"
    assert len(captured_payloads) == 1
    body = captured_payloads[0]
    riff_idx = body.index(b"RIFF")
    wav_end = body.index(b"\r\n--", riff_idx)
    with wave.open(io.BytesIO(body[riff_idx:wav_end]), "rb") as wf:
        assert wf.getframerate() == 48_000
        assert wf.getnchannels() == 2
        assert wf.readframes(100) == pcm_48k_stereo
