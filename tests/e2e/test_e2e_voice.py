"""Pre-staged E2E tests for Voice Interaction (F7) and Voice+Chat/UI (T3-06, T3-07)."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from max_core.gateway.adapters import HTTPAdapter
from max_core.gateway.contracts import (
    ChatMessage,
    Dialect,
    GatewayRequest,
    InferenceDestination,
    ModelSpec,
    Privacy,
)
from max_core.gateway.router import Gateway
from max_core.local_config import DesktopConfig, load_desktop_config
from max_core.native_desktop.backend import NativeDesktopCore
from max_core.persona import VoicePreference, with_persona
from max_core.presence.notifications import FocusState
from max_core.presence.orb import OrbError, OrbEvent, OrbReducer, OrbState, OrbVisibility
from max_core.voice.barge_in import BargeInGate
from max_core.voice.local_activation import ActivationError, ActivationMode, LocalActivationGate
from max_core.voice.pipeline import (
    MAX_INPUT_AUDIO_BYTES,
    MAX_INPUT_FRAME_BYTES,
    MAX_TRANSCRIPT_CHARS,
    VoiceError,
    VoiceTurnPipeline,
)
from tests.e2e.conftest import (
    FakeMicrophoneRecorder,
    FakeSpeechToText,
    FakeTextToSpeech,
    MockLLMServer,
    RecordingAudioSink,
    make_fake_desktop_tts,
    synthetic_audio_stream,
)


def _make_voice_controller(
    config: DesktopConfig,
    *,
    tts: Any | None = None,
    stt: Any | None = None,
    recorder: Any | None = None,
    headless: bool | None = None,
    chat_service: Any | None = None,
    activation_gate: LocalActivationGate | None = None,
    barge_in_gate: BargeInGate | None = None,
    clock_ms: Callable[[], int] | None = None,
    focus_probe: Callable[[], FocusState] | None = None,
) -> Any:
    from max_core.voice.controller import VoiceController

    sig = inspect.signature(VoiceController.__init__)
    kwargs: dict[str, Any] = {}
    candidates: dict[str, Any] = {
        "config": config,
        "headless": headless,
        "tts": tts,
        "stt": stt,
        "recorder": recorder,
        "chat_service": chat_service,
        "activation_gate": activation_gate,
        "barge_in_gate": barge_in_gate,
        "clock_ms": clock_ms,
        "focus_probe": focus_probe,
    }
    for name, val in candidates.items():
        if val is not None and name in sig.parameters:
            kwargs[name] = val
    return VoiceController(**kwargs)


def _pump_until(app: Any, predicate: Callable[[], bool], *, timeout_s: float = 4.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    app.processEvents()
    assert predicate(), "Timed out waiting for Qt UI update"


# ---------------------------------------------------------------------------
# Tier 1 — F7: Voice Capabilities, Push-to-Talk, TTS, Barge-In & Headless
# ---------------------------------------------------------------------------


def test_t1_f7_01_voice_controller_capabilities_without_auto_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1-F7-01: VoiceController.capabilities() inspects environment without auto-recording."""
    from max_core.voice.controller import VoiceCapabilities

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "voice_caps.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    controller = _make_voice_controller(cfg)
    caps = controller.capabilities()
    assert isinstance(caps, VoiceCapabilities)
    assert isinstance(caps.tts_available, bool)
    assert isinstance(caps.tts_engine, str) and caps.tts_engine
    assert isinstance(caps.stt_available, bool)
    assert isinstance(caps.mic_available, bool)
    assert caps.headless is True
    assert caps.mic_available is False


def test_t1_f7_02_push_to_talk_activation_and_mock_stt_transcription(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_llm_server: MockLLMServer,
) -> None:
    """T1-F7-02: Armed push-to-talk captures gesture and transcribes via mock STT endpoint."""
    from max_core.voice.desktop_stt import DesktopSpeechToText

    monkeypatch.setenv("QT_QPA_PLATFORM", "x")
    mock_llm_server.queue_text("Check today's task queue.")

    gate = LocalActivationGate(mode=ActivationMode.PUSH_TO_TALK)
    gate.arm(owner_opted_in=True, os_microphone_permission=True, foreground=True)
    assert gate.push_to_talk(direct_user_gesture=True, now_ms=1_000) is True
    assert gate.streaming_allowed(now_ms=1_500) is True

    cfg = load_desktop_config(
        env={
            "MAX_LOCAL_DB_PATH": str(tmp_path / "voice_ptt.sqlite3"),
            "OPENAI_API_KEY": "sk-test-stt-1234567890",
        },
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )

    stt_sig = inspect.signature(DesktopSpeechToText.__init__)
    stt_kwargs: dict[str, Any] = {}
    http_client: httpx.AsyncClient | None = None
    if "api_key" in stt_sig.parameters:
        stt_kwargs["api_key"] = "sk-test-stt-1234567890"
    if "endpoint" in stt_sig.parameters:
        stt_kwargs["endpoint"] = mock_llm_server.stt_endpoint
    if "headless" in stt_sig.parameters:
        stt_kwargs["headless"] = False
    if "transport" in stt_sig.parameters:
        stt_kwargs["transport"] = mock_llm_server.as_mock_transport()
    elif "http_client" in stt_sig.parameters:
        http_client = httpx.AsyncClient(transport=mock_llm_server.as_mock_transport())
        stt_kwargs["http_client"] = http_client
    stt = DesktopSpeechToText(**stt_kwargs)

    try:
        transcript = asyncio.run(stt.recognize(synthetic_audio_stream()))
    finally:
        if http_client is not None:
            asyncio.run(http_client.aclose())
    assert transcript == "Check today's task queue."
    assert len(mock_llm_server.requests) == 1
    assert mock_llm_server.requests[0].path.endswith("/v1/audio/transcriptions")

    controller = _make_voice_controller(
        cfg,
        tts=make_fake_desktop_tts(
            VoicePreference(presentation=cfg.voice_presentation, locale=cfg.voice_locale)
        ),
        stt=FakeSpeechToText("Check today's task queue."),
        recorder=FakeMicrophoneRecorder(chunks=[b"\x01\x02" * 160]),
        activation_gate=gate,
    )
    caps = controller.capabilities()
    assert caps.headless is False
    assert caps.stt_available is True
    assert caps.mic_available is True
    assert controller.start_push_to_talk(direct_user_gesture=True) is True
    spoken = controller.stop_push_to_talk_and_transcribe()
    assert spoken == "Check today's task queue."


def test_t1_f7_03_desktop_tts_honors_feminine_en_gb_preference(
    tmp_path: Path,
) -> None:
    """T1-F7-03: DesktopTextToSpeech selects feminine en-GB voice and speaks cleanly."""
    from max_core.voice.desktop_tts import DesktopTextToSpeech

    class _FakeLocale:
        def __init__(self, bcp47: str) -> None:
            self._bcp47 = bcp47

        def name(self) -> str:
            return self._bcp47.replace("-", "_")

        def bcp47Name(self) -> str:
            return self._bcp47

    class _FakeVoice:
        def __init__(self, name: str, bcp47: str, gender: str) -> None:
            self._name = name
            self._locale = _FakeLocale(bcp47)
            self._gender = gender

        def name(self) -> str:
            return self._name

        def locale(self) -> _FakeLocale:
            return self._locale

        def gender(self) -> str:
            return self._gender

    class _FakeDriver:
        def __init__(self) -> None:
            self.spoken: list[str] = []
            self.selected_voice: Any = None
            self.selected_locale: Any = None
            self._voices = [
                _FakeVoice("en-US-male", "en-US", "Male"),
                _FakeVoice("en-GB-female", "en-GB", "Female"),
            ]

        def engine(self) -> str:
            return "sapi"

        def availableVoices(self) -> list[_FakeVoice]:
            return list(self._voices)

        def allVoices(self, _locale: Any) -> list[_FakeVoice]:
            return list(self._voices)

        def setLocale(self, locale: Any) -> None:
            self.selected_locale = locale

        def setVoice(self, voice: Any) -> None:
            self.selected_voice = voice

        def say(self, text: str) -> None:
            self.spoken.append(text)

        def stop(self, _hint: Any = None) -> None:
            pass

        def state(self) -> str:
            return "Ready"

        def errorReason(self) -> str:
            return "NoError"

    class _FakeDevice:
        def isNull(self) -> bool:
            return False

    driver = _FakeDriver()
    pref = VoicePreference(presentation="feminine", locale="en-GB")
    tts = DesktopTextToSpeech(
        preference=pref,
        headless=False,
        engine_factory=lambda _name: driver,
        available_engines_fn=lambda: ["sapi"],
        audio_outputs_fn=lambda: [_FakeDevice()],
    )
    assert tts.available is True
    assert tts.engine_name == "sapi"
    assert tts.selected_voice_name == "en-GB-female"

    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "voice_tts.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    controller = _make_voice_controller(
        cfg,
        tts=tts,
        stt=FakeSpeechToText("unused"),
        recorder=FakeMicrophoneRecorder(),
    )
    assert controller.speak_text("Good morning. All local checks are ready.") is True
    assert driver.spoken == ["Good morning. All local checks are ready."]


def test_t1_f7_04_voice_pipeline_barge_in_cancels_playback_and_stops_sink(
    mock_llm_server: MockLLMServer,
) -> None:
    """T1-F7-04: Verified 3-frame barge-in interrupts VoiceTurnPipeline and calls sink.stop()."""
    mock_llm_server.queue_text(
        "Sentence one is playing. Sentence two continues. Sentence three finishes.",
        stream=True,
    )

    async def _run() -> tuple[Any, RecordingAudioSink]:
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
            api_key="sk-test-barge-1234567890",
        )
        interrupt = asyncio.Event()
        gate = BargeInGate(minimum_voice_frames=3, threshold=0.7)

        def _on_play(count: int) -> None:
            if count == 1:
                assert (
                    gate.observe(
                        0.85,
                        microphone_enabled=True,
                        playback_active=True,
                        echo_suppression_verified=True,
                    )
                    is False
                )
                assert (
                    gate.observe(
                        0.85,
                        microphone_enabled=True,
                        playback_active=True,
                        echo_suppression_verified=True,
                    )
                    is False
                )
                if gate.observe(
                    0.85,
                    microphone_enabled=True,
                    playback_active=True,
                    echo_suppression_verified=True,
                ):
                    interrupt.set()

        sink = RecordingAudioSink(on_play=_on_play)
        tts = FakeTextToSpeech(chunks_per_sentence=3, delay_seconds=0.05)
        stt = FakeSpeechToText("Summarise my tasks")

        async with httpx.AsyncClient(trust_env=False) as http_client:
            gw = Gateway([spec], HTTPAdapter(http_client))
            pipeline = VoiceTurnPipeline(gw)
            res = await pipeline.run(
                synthetic_audio_stream(),
                stt,
                lambda text: with_persona(
                    GatewayRequest(
                        request_id="voice-barge-1",
                        messages=(ChatMessage("user", text),),
                        privacy=Privacy.INTERNAL,
                        max_input_tokens=4096,
                        max_output_tokens=256,
                        max_cost_usd=Decimal("0.05"),
                        cloud_allowed=True,
                    )
                ),
                tts,
                sink,
                interrupt,
            )
        return res, sink

    result, sink = asyncio.run(_run())
    assert result.interrupted is True
    assert sink.stop_calls >= 1


def test_t1_f7_05_headless_offscreen_voice_degrades_gracefully_without_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T1-F7-05: Under QT_QPA_PLATFORM=offscreen, VoiceController degrades cleanly with no crash."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "voice_headless.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    controller = _make_voice_controller(cfg)
    caps = controller.capabilities()
    assert caps.headless is True
    assert caps.mic_available is False
    assert controller.start_push_to_talk() is False
    assert controller.stop_push_to_talk_and_transcribe() is None
    assert controller.speak_text("Silent in headless mode") is False
    controller.interrupt_speech()


# ---------------------------------------------------------------------------
# Tier 2 — F7: Voice Consent, Barge-In Echo Defence, Limits & Clock/Replay
# ---------------------------------------------------------------------------


def test_t2_f7_01_local_activation_gate_denies_missing_consent_permission_or_background() -> None:
    """T2-F7-01: LocalActivationGate fails closed unless opt-in, OS mic, and foreground hold."""
    gate = LocalActivationGate(mode=ActivationMode.PUSH_TO_TALK)
    for opt_in, perm, fg in (
        (False, True, True),
        (True, False, True),
        (True, True, False),
    ):
        with pytest.raises(ActivationError, match="microphone_or_consent_denied"):
            gate.arm(owner_opted_in=opt_in, os_microphone_permission=perm, foreground=fg)
        assert gate.streaming_allowed(now_ms=100) is False


def test_t2_f7_02_non_gesture_ptt_and_revoke_halt_voice_streaming(
    tmp_path: Path,
) -> None:
    """T2-F7-02: Non-direct push_to_talk returns False and revoke() immediately stops streaming."""
    gate = LocalActivationGate(mode=ActivationMode.PUSH_TO_TALK)
    gate.arm(owner_opted_in=True, os_microphone_permission=True, foreground=True)
    assert gate.push_to_talk(direct_user_gesture=False, now_ms=1_000) is False
    assert gate.streaming_allowed(now_ms=1_100) is False

    assert gate.push_to_talk(direct_user_gesture=True, now_ms=1_200) is True
    assert gate.streaming_allowed(now_ms=1_300) is True
    gate.revoke()
    assert gate.streaming_allowed(now_ms=1_400) is False

    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "voice_revoke.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    controller = _make_voice_controller(
        cfg,
        tts=make_fake_desktop_tts(),
        stt=FakeSpeechToText("hello"),
        recorder=FakeMicrophoneRecorder(),
        activation_gate=gate,
    )
    assert controller.start_push_to_talk(direct_user_gesture=False) is False
    assert controller.last_error == "activation_denied"
    assert controller.start_push_to_talk(direct_user_gesture=True) is True
    controller.revoke_microphone()
    assert controller.is_recording is False
    assert gate.streaming_allowed(now_ms=1_400) is False


def test_t2_f7_03_barge_in_gate_ignores_unverified_echo_or_too_few_frames() -> None:
    """T2-F7-03: BargeInGate refuses echo without verified AEC or <3 consecutive VAD frames."""
    gate = BargeInGate(minimum_voice_frames=3, threshold=0.7)
    for _ in range(5):
        assert (
            gate.observe(
                0.99,
                microphone_enabled=True,
                playback_active=True,
                echo_suppression_verified=False,
            )
            is False
        )
        assert (
            gate.observe(
                0.99,
                microphone_enabled=False,
                playback_active=True,
                echo_suppression_verified=True,
            )
            is False
        )

    assert (
        gate.observe(
            0.95,
            microphone_enabled=True,
            playback_active=True,
            echo_suppression_verified=True,
        )
        is False
    )
    assert (
        gate.observe(
            0.95,
            microphone_enabled=True,
            playback_active=True,
            echo_suppression_verified=True,
        )
        is False
    )
    assert (
        gate.observe(
            0.50,
            microphone_enabled=True,
            playback_active=True,
            echo_suppression_verified=True,
        )
        is False
    )
    # Counter reset by 0.50 frame, so another single high frame is still False.
    assert (
        gate.observe(
            0.95,
            microphone_enabled=True,
            playback_active=True,
            echo_suppression_verified=True,
        )
        is False
    )


def test_t2_f7_04_voice_pipeline_rejects_empty_oversized_transcript_and_audio_limits(
    mock_llm_server: MockLLMServer,
) -> None:
    """T2-F7-04: VoiceTurnPipeline rejects empty/oversized transcript and >256KB/>8MB audio."""

    async def _run() -> None:
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
            api_key="sk-test-vlimits-1234567890",
        )
        async with httpx.AsyncClient(trust_env=False) as http_client:
            gw = Gateway([spec], HTTPAdapter(http_client))
            pipeline = VoiceTurnPipeline(gw)

            def _build_req(t: str) -> GatewayRequest:
                return with_persona(
                    GatewayRequest(
                        request_id="voice-limit",
                        messages=(ChatMessage("user", t),),
                        privacy=Privacy.INTERNAL,
                        max_input_tokens=4096,
                        max_output_tokens=128,
                        max_cost_usd=Decimal("0.05"),
                        cloud_allowed=True,
                    )
                )

            with pytest.raises(VoiceError, match="empty_transcript"):
                await pipeline.run(
                    synthetic_audio_stream(),
                    FakeSpeechToText("   "),
                    _build_req,
                    FakeTextToSpeech(),
                    RecordingAudioSink(),
                    asyncio.Event(),
                )

            with pytest.raises(VoiceError, match="transcript_too_large"):
                await pipeline.run(
                    synthetic_audio_stream(),
                    FakeSpeechToText("a" * (MAX_TRANSCRIPT_CHARS + 1)),
                    _build_req,
                    FakeTextToSpeech(),
                    RecordingAudioSink(),
                    asyncio.Event(),
                )

            with pytest.raises(VoiceError, match="invalid_audio_frame"):
                await pipeline.run(
                    synthetic_audio_stream((b"x" * (MAX_INPUT_FRAME_BYTES + 1),)),
                    FakeSpeechToText("ok"),
                    _build_req,
                    FakeTextToSpeech(),
                    RecordingAudioSink(),
                    asyncio.Event(),
                )

            async def _huge_audio() -> AsyncIterator[bytes]:
                chunk = b"a" * MAX_INPUT_FRAME_BYTES
                count = (MAX_INPUT_AUDIO_BYTES // MAX_INPUT_FRAME_BYTES) + 1
                for _ in range(count):
                    yield chunk

            with pytest.raises(VoiceError, match="audio_limit_exceeded"):
                await pipeline.run(
                    _huge_audio(),
                    FakeSpeechToText("ok"),
                    _build_req,
                    FakeTextToSpeech(),
                    RecordingAudioSink(),
                    asyncio.Event(),
                )

    asyncio.run(_run())
    assert len(mock_llm_server.requests) == 0


def test_t2_f7_05_stt_http_errors_and_network_failure_degrade_cleanly(
    tmp_path: Path,
    mock_llm_server: MockLLMServer,
) -> None:
    """T2-F7-05: STT endpoint HTTP 401/500 and ConnectError degrade cleanly without crashing."""
    from max_core.voice.desktop_stt import DesktopSpeechToText

    mock_llm_server.queue_error(401)
    mock_llm_server.queue_error(500)
    mock_llm_server.queue_error(0, raise_connect_error=True)

    stt_sig = inspect.signature(DesktopSpeechToText.__init__)
    stt_kwargs: dict[str, Any] = {}
    http_client: httpx.AsyncClient | None = None
    if "api_key" in stt_sig.parameters:
        stt_kwargs["api_key"] = "sk-test-stt-fail-123456"
    if "endpoint" in stt_sig.parameters:
        stt_kwargs["endpoint"] = mock_llm_server.stt_endpoint
    if "headless" in stt_sig.parameters:
        stt_kwargs["headless"] = False
    if "transport" in stt_sig.parameters:
        stt_kwargs["transport"] = mock_llm_server.as_mock_transport()
    elif "http_client" in stt_sig.parameters:
        http_client = httpx.AsyncClient(transport=mock_llm_server.as_mock_transport())
        stt_kwargs["http_client"] = http_client
    stt = DesktopSpeechToText(**stt_kwargs)

    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "voice_stt_err.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    controller = _make_voice_controller(
        cfg,
        tts=make_fake_desktop_tts(),
        stt=stt,
        recorder=FakeMicrophoneRecorder(),
    )
    try:
        for _ in range(3):
            with pytest.raises(VoiceError):
                asyncio.run(stt.recognize(synthetic_audio_stream()))
    finally:
        if http_client is not None:
            asyncio.run(http_client.aclose())
    assert controller.stop_push_to_talk_and_transcribe() is None


def test_t2_f7_06_clock_regression_and_replayed_orb_event_rejected() -> None:
    """T2-F7-06: Monotonic clock regression revokes gate; stale/replayed OrbEvent raises OrbError."""
    gate = LocalActivationGate(mode=ActivationMode.WAKE_WORD)
    gate.arm(owner_opted_in=True, os_microphone_permission=True, foreground=True)
    assert (
        gate.observe_local_scores(
            wake_probability=0.9,
            vad_probability=0.9,
            local_inference_verified=True,
            now_ms=2_000,
        )
        is False
    )
    with pytest.raises(ActivationError, match="monotonic_clock_regressed"):
        gate.streaming_allowed(now_ms=1_500)
    assert gate.streaming_allowed(now_ms=2_500) is False

    orb = OrbReducer(device_id="win-desktop-1")
    orb.receive(
        OrbEvent(
            device_id="win-desktop-1", sequence=2, issued_at_ms=5_000, state=OrbState.LISTENING
        ),
        authenticated=True,
        now_ms=5_100,
    )
    with pytest.raises(OrbError, match="stale_or_replayed_orb_event"):
        orb.receive(
            OrbEvent(
                device_id="win-desktop-1",
                sequence=2,
                issued_at_ms=5_150,
                state=OrbState.THINKING,
            ),
            authenticated=True,
            now_ms=5_200,
        )


# ---------------------------------------------------------------------------
# Tier 3 — F4+F7 and F7+F3 Cross-Feature Combinations
# ---------------------------------------------------------------------------


def test_t3_06_voice_stt_to_cloud_chat_to_tts_pipeline_round_trip(
    mock_llm_server: MockLLMServer,
) -> None:
    """T3-06: STT transcript routes through cloud Gateway into TTS + RecordingAudioSink."""
    mock_llm_server.queue_text(
        "Here is your briefing. One task is queued.",
        stream=True,
    )
    pref = VoicePreference(presentation="feminine", locale="en-GB")
    assert pref.presentation == "feminine" and pref.locale == "en-GB"

    async def _run() -> tuple[Any, FakeTextToSpeech, RecordingAudioSink]:
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
            api_key="sk-test-t306-1234567890",
        )
        stt = FakeSpeechToText("Give me my briefing")
        tts = FakeTextToSpeech(chunks_per_sentence=2)
        sink = RecordingAudioSink()
        async with httpx.AsyncClient(trust_env=False) as http_client:
            gw = Gateway([spec], HTTPAdapter(http_client))
            pipeline = VoiceTurnPipeline(gw)
            res = await pipeline.run(
                synthetic_audio_stream(),
                stt,
                lambda text: with_persona(
                    GatewayRequest(
                        request_id="t3-06-turn",
                        messages=(ChatMessage("user", text),),
                        privacy=Privacy.INTERNAL,
                        max_input_tokens=4096,
                        max_output_tokens=256,
                        max_cost_usd=Decimal("0.05"),
                        cloud_allowed=True,
                    )
                ),
                tts,
                sink,
                asyncio.Event(),
            )
        return res, tts, sink

    result, tts, sink = asyncio.run(_run())
    assert result.interrupted is False
    assert result.speech_chunks == 2
    assert result.audio_frames == 4
    assert tts.spoken_sentences == ["Here is your briefing.", "One task is queued."]
    assert len(sink.played_frames) == 4
    assert len(mock_llm_server.requests) == 1


def test_t3_07_orb_state_transitions_and_quiet_mode_ui_revokes_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    desktop_core_factory: Callable[..., NativeDesktopCore],
) -> None:
    """T3-07: OrbReducer transitions across voice states; Quiet mode forces PASSIVE & revokes mic."""
    pytest.importorskip("PySide6")
    from PySide6 import QtWidgets

    from max_core.native_desktop.qt_ui import MaxWindow

    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    orb = OrbReducer(device_id="desktop-1")
    gate = LocalActivationGate(mode=ActivationMode.PUSH_TO_TALK)
    gate.arm(owner_opted_in=True, os_microphone_permission=True, foreground=True)
    assert gate.push_to_talk(direct_user_gesture=True, now_ms=1_000) is True

    for seq, state in enumerate(
        (OrbState.LISTENING, OrbState.THINKING, OrbState.SPEAKING, OrbState.IDLE), start=1
    ):
        now = 1_000 + seq * 100
        orb.receive(
            OrbEvent(device_id="desktop-1", sequence=seq, issued_at_ms=now, state=state),
            authenticated=True,
            now_ms=now,
        )
        view = orb.view(
            now_ms=now + 10,
            owner_enabled=True,
            foreground=True,
            quiet_mode=False,
            focus=FocusState.ACCEPTS_NOTIFICATIONS,
        )
        assert view.state == state
        assert view.visibility == OrbVisibility.ACTIVE

    passive_view = orb.view(
        now_ms=1_500,
        owner_enabled=True,
        foreground=True,
        quiet_mode=True,
        focus=FocusState.ACCEPTS_NOTIFICATIONS,
    )
    assert passive_view.visibility == OrbVisibility.PASSIVE

    cfg = load_desktop_config(
        env={"MAX_LOCAL_DB_PATH": str(tmp_path / "t3_07.sqlite3")},
        dotenv_path=tmp_path / "missing.env",
        config_file=tmp_path / "missing.json",
        data_dir=tmp_path,
    )
    core = desktop_core_factory(config=cfg)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MaxWindow(core)
    try:
        window.show()
        _pump_until(app, lambda: "checking" not in window.system_storage.text().lower())
        window.toggle_quiet()
        gate.revoke()
        assert window.orb.quiet is True
        assert gate.streaming_allowed(now_ms=2_000) is False
    finally:
        window.close()
        app.processEvents()
