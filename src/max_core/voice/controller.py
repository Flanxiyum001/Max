"""Desktop voice session controller coordinating STT, TTS, gates, and Orb state.

Microphone capture is strictly opt-in via push-to-talk user gesture and gated by
``LocalActivationGate``. Active speech is immediately interruptible via ``BargeInGate``
and ``interrupt_speech()``. All STT transcripts are returned strictly as untrusted
user text and never trigger tool execution directly.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from max_core.local_config import DesktopConfig
from max_core.persona import VoicePreference
from max_core.presence.notifications import FocusState
from max_core.presence.orb import OrbEvent, OrbReducer, OrbState, OrbView
from max_core.presence.windows import windows_focus_state
from max_core.voice.barge_in import BargeInGate
from max_core.voice.desktop_stt import (
    DEFAULT_CHANNELS,
    DEFAULT_SAMPLE_RATE,
    DesktopSpeechToText,
    MicrophoneRecorder,
    QtMicrophoneRecorder,
    pcm16_to_wav,
)
from max_core.voice.desktop_tts import DesktopAudioSink, DesktopTextToSpeech
from max_core.voice.local_activation import ActivationError, ActivationMode, LocalActivationGate
from max_core.voice.pipeline import (
    MAX_INPUT_AUDIO_BYTES,
    MAX_INPUT_FRAME_BYTES,
    MAX_TRANSCRIPT_CHARS,
    AudioSink,
    TextGateway,
    VoiceError,
    VoiceTurnPipeline,
    VoiceTurnResult,
    bounded_microphone,
)

if TYPE_CHECKING:
    from max_core.gateway.contracts import GatewayRequest


@dataclass(frozen=True, slots=True)
class VoiceCapabilities:
    """Snapshot of desktop voice hardware and STT/TTS readiness."""

    tts_available: bool
    tts_engine: str
    stt_available: bool
    mic_available: bool
    headless: bool
    reason: str | None


def _default_clock_ms() -> int:
    return time.monotonic_ns() // 1_000_000


async def _iter_frames(frames: list[bytes]) -> AsyncIterator[bytes]:
    try:
        for idx, frame in enumerate(frames):
            frames[idx] = b""
            if frame:
                yield frame
    finally:
        frames.clear()


class _TurnAudioSink:
    """``AudioSink`` wrapper transitioning Orb state to ``SPEAKING`` on first audio frame."""

    def __init__(self, inner: AudioSink, on_first_frame: Callable[[], None]) -> None:
        self._inner = inner
        self._on_first_frame = on_first_frame
        self._started = False

    async def play(self, frame: bytes) -> None:
        if not self._started:
            self._started = True
            self._on_first_frame()
        await self._inner.play(frame)

    async def stop(self) -> None:
        await self._inner.stop()


class VoiceController:
    """Coordinates desktop TTS, STT, microphone activation, barge-in, and Orb state."""

    def __init__(
        self,
        *,
        config: DesktopConfig | None = None,
        device_id: str = "desktop",
        headless: bool | None = None,
        tts: DesktopTextToSpeech | None = None,
        sink: DesktopAudioSink | None = None,
        stt: DesktopSpeechToText | None = None,
        recorder: MicrophoneRecorder | None = None,
        gateway: TextGateway | None = None,
        activation_gate: LocalActivationGate | None = None,
        barge_in_gate: BargeInGate | None = None,
        orb: OrbReducer | None = None,
        clock_ms: Callable[[], int] | None = None,
        focus_probe: Callable[[], FocusState] | None = None,
        owner_opted_in: bool = True,
        allow_remote_transcription: bool = False,
        os_microphone_permission: bool = True,
        foreground: bool = True,
    ) -> None:
        preference = (
            VoicePreference(
                presentation=config.voice_presentation,
                locale=config.voice_locale,
            )
            if config is not None
            else VoicePreference()
        )
        self._tts = (
            tts
            if tts is not None
            else DesktopTextToSpeech(preference=preference, headless=headless)
        )
        self._sink = sink if sink is not None else DesktopAudioSink(self._tts)
        self._stt = stt if stt is not None else DesktopSpeechToText(desktop_config=config)
        self._recorder: MicrophoneRecorder = (
            recorder if recorder is not None else QtMicrophoneRecorder(headless=headless)
        )
        self._gateway = gateway
        self._activation = (
            activation_gate
            if activation_gate is not None
            else LocalActivationGate(ActivationMode.PUSH_TO_TALK)
        )
        self._barge_in = barge_in_gate if barge_in_gate is not None else BargeInGate()
        self._device_id = device_id
        self._orb = orb if orb is not None else OrbReducer(device_id=device_id)
        self._clock_ms = clock_ms if clock_ms is not None else _default_clock_ms
        self._focus_probe = focus_probe if focus_probe is not None else windows_focus_state

        self._owner_opted_in = bool(owner_opted_in)
        self._remote_stt_consent = allow_remote_transcription is True
        self._os_microphone_permission = bool(os_microphone_permission)
        self._foreground = bool(foreground)

        self._lock = threading.RLock()
        self._recording = False
        self._pipeline_playback_active = False
        self._tts_playback_active = False
        self._interrupt_event = asyncio.Event()
        self._interrupt_loop: asyncio.AbstractEventLoop | None = None
        self._orb_seq = 0
        self._last_orb_ms: int | None = None
        self._last_error: str | None = None

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    def _is_speaking_locked(self) -> bool:
        tts_speaking = self._tts.is_speaking()
        if self._tts_playback_active and self._tts.available and not tts_speaking:
            self._tts_playback_active = False
        return self._pipeline_playback_active or self._tts_playback_active or tts_speaking

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            return self._is_speaking_locked()

    @property
    def tts(self) -> DesktopTextToSpeech:
        return self._tts

    @property
    def stt(self) -> DesktopSpeechToText:
        return self._stt

    @property
    def recorder(self) -> MicrophoneRecorder:
        return self._recorder

    def _now_ms(self) -> int:
        now = self._clock_ms()
        if type(now) is not int or now < 0:
            raise VoiceError("invalid_monotonic_clock")
        return now

    def _emit_orb(self, state: OrbState, *, now_ms: int | None = None) -> None:
        with self._lock:
            now = self._now_ms() if now_ms is None else now_ms
            orb_now = (
                self._last_orb_ms
                if self._last_orb_ms is not None and now < self._last_orb_ms
                else now
            )
            self._last_orb_ms = orb_now
            self._orb_seq += 1
            self._orb.receive(
                OrbEvent(self._device_id, self._orb_seq, orb_now, state),
                authenticated=True,
                now_ms=orb_now,
            )

    def capabilities(self) -> VoiceCapabilities:
        headless = self._tts.headless or self._recorder.headless
        reasons: list[str] = []
        if headless:
            reasons.append("headless_offscreen")
        else:
            if not self._tts.available and self._tts.reason:
                reasons.append(self._tts.reason)
            if not self._recorder.available and self._recorder.reason:
                reasons.append(self._recorder.reason)
            if not self._stt.available and self._stt.reason:
                reasons.append(self._stt.reason)
        deduped = list(dict.fromkeys(reasons))
        reason = "; ".join(deduped) if deduped else None
        return VoiceCapabilities(
            tts_available=self._tts.available,
            tts_engine=self._tts.engine_name,
            stt_available=self._stt.available,
            mic_available=self._recorder.available,
            headless=headless,
            reason=reason,
        )

    def set_presence_flags(
        self,
        *,
        owner_opted_in: bool | None = None,
        os_microphone_permission: bool | None = None,
        foreground: bool | None = None,
    ) -> None:
        with self._lock:
            if owner_opted_in is not None:
                self._owner_opted_in = bool(owner_opted_in)
            if os_microphone_permission is not None:
                self._os_microphone_permission = bool(os_microphone_permission)
            if foreground is not None:
                self._foreground = bool(foreground)
            if not (self._owner_opted_in and self._os_microphone_permission and self._foreground):
                self.revoke_microphone()

    def revoke_microphone(self) -> None:
        with self._lock:
            self._recording = False
            self._activation.revoke()
            self._recorder.discard()
            self.interrupt_speech()

    @property
    def remote_stt_consent(self) -> bool:
        with self._lock:
            return self._remote_stt_consent

    def grant_remote_stt_consent(self) -> None:
        """Trusted UI calls this only after an explicit, visible owner decision."""
        with self._lock:
            self._remote_stt_consent = True

    def revoke_remote_stt_consent(self) -> None:
        with self._lock:
            self._remote_stt_consent = False

    def start_push_to_talk(self, *, direct_user_gesture: bool = True) -> bool:
        with self._lock:
            if self._recording:
                return False
            if direct_user_gesture is not True:
                self._last_error = "activation_denied"
                return False
            if not self._recorder.available:
                self._last_error = self._recorder.reason or "microphone_unavailable"
                return False
            if not self._stt.available:
                self._last_error = self._stt.reason or "stt_not_configured"
                return False
            if (
                isinstance(self._stt, DesktopSpeechToText)
                and self._stt.requires_remote_upload
                and not self._remote_stt_consent
            ):
                self._last_error = "remote_stt_consent_required"
                return False

            try:
                now = self._now_ms()
                self.interrupt_speech()
                self._activation.arm(
                    owner_opted_in=self._owner_opted_in,
                    os_microphone_permission=self._os_microphone_permission,
                    foreground=self._foreground,
                )
                activated = self._activation.push_to_talk(
                    direct_user_gesture=direct_user_gesture,
                    now_ms=now,
                )
                allowed = activated and self._activation.streaming_allowed(now_ms=now)
            except (ActivationError, VoiceError) as exc:
                self._activation.revoke()
                self._last_error = str(exc)
                return False

            if not allowed:
                self._activation.revoke()
                self._last_error = "activation_denied"
                return False

            if not self._recorder.start():
                self._activation.revoke()
                self._last_error = "microphone_start_failed"
                return False

            self._recording = True
            self._last_error = None
            self._emit_orb(OrbState.LISTENING, now_ms=now)
            return True

    async def stop_push_to_talk_and_transcribe_async(self) -> str | None:
        with self._lock:
            if not self._recording:
                return None
            self._recording = False

            allowed = False
            now: int | None = None
            try:
                now = self._now_ms()
                allowed = self._activation.streaming_allowed(now_ms=now)
                if (
                    isinstance(self._stt, DesktopSpeechToText)
                    and self._stt.requires_remote_upload
                    and not self._remote_stt_consent
                ):
                    allowed = False
                    self._last_error = "remote_stt_consent_required"
            except (ActivationError, VoiceError) as exc:
                self._last_error = str(exc)
                allowed = False
            finally:
                self._activation.stop_turn()

        frames: list[bytes] = []
        idle_emitted = False
        try:
            frames = self._recorder.stop_and_drain()
            with self._lock:
                if not allowed:
                    self._last_error = self._last_error or "activation_expired_or_revoked"
                    self._emit_orb(OrbState.IDLE, now_ms=now)
                    idle_emitted = True
                    return None
                if not frames:
                    self._last_error = "empty_audio"
                    self._emit_orb(OrbState.IDLE, now_ms=now)
                    idle_emitted = True
                    return None

                rec_rate = self._recorder.sample_rate
                rec_channels = self._recorder.channels
                first_frame = frames[0]
                is_wav = (
                    len(first_frame) >= 12
                    and first_frame[:4] == b"RIFF"
                    and first_frame[8:12] == b"WAVE"
                )
                if not is_wav and (
                    rec_rate != DEFAULT_SAMPLE_RATE or rec_channels != DEFAULT_CHANNELS
                ):
                    pcm_payload = b"".join(frames)
                    if len(pcm_payload) + 44 <= MAX_INPUT_AUDIO_BYTES:
                        wav_payload = pcm16_to_wav(
                            pcm_payload,
                            sample_rate=rec_rate,
                            channels=rec_channels,
                        )
                        frames = [
                            wav_payload[offset : offset + MAX_INPUT_FRAME_BYTES]
                            for offset in range(0, len(wav_payload), MAX_INPUT_FRAME_BYTES)
                        ]

                self._emit_orb(OrbState.THINKING, now_ms=now)

            transcript = await self._stt.recognize(bounded_microphone(_iter_frames(frames)))
            if not isinstance(transcript, str):
                with self._lock:
                    self._last_error = "empty_transcript"
                return None
            cleaned = transcript.strip()
            if not cleaned:
                with self._lock:
                    self._last_error = "empty_transcript"
                return None
            if len(cleaned) > MAX_TRANSCRIPT_CHARS:
                with self._lock:
                    self._last_error = "transcript_too_large"
                return None
            with self._lock:
                self._last_error = None
            return cleaned
        except VoiceError as exc:
            with self._lock:
                self._last_error = str(exc)
            return None
        finally:
            frames.clear()
            if not idle_emitted:
                with self._lock:
                    self._emit_orb(OrbState.IDLE)

    def stop_push_to_talk_and_transcribe(self) -> str | None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.stop_push_to_talk_and_transcribe_async())
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, self.stop_push_to_talk_and_transcribe_async())
            return future.result()

    def speak_text(self, text: str) -> bool:
        if not isinstance(text, str) or not text.strip():
            return False
        with self._lock:
            if not self._tts.available:
                return False
            self._barge_in.reset()
            self._emit_orb(OrbState.SPEAKING)
            started = self._tts.speak(text.strip())
            self._tts_playback_active = started
            if not started:
                self._emit_orb(OrbState.IDLE)
            return started

    def interrupt_speech(self) -> None:
        with self._lock:
            event = self._interrupt_event
            loop = self._interrupt_loop
            if loop is not None and loop.is_running():
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is loop:
                    event.set()
                else:
                    try:
                        loop.call_soon_threadsafe(event.set)
                    except RuntimeError:
                        event.set()
            else:
                event.set()
            self._tts.stop()
            self._pipeline_playback_active = False
            self._tts_playback_active = False
            self._barge_in.reset()
            self._emit_orb(OrbState.IDLE)

    def observe_barge_in(
        self,
        vad_probability: float,
        *,
        echo_suppression_verified: bool,
    ) -> bool:
        with self._lock:
            microphone_enabled = (
                self._owner_opted_in
                and self._os_microphone_permission
                and self._foreground
                and self._recorder.available
            )
            playback_active = self._is_speaking_locked()
            triggered = self._barge_in.observe(
                vad_probability,
                microphone_enabled=microphone_enabled,
                playback_active=playback_active,
                echo_suppression_verified=echo_suppression_verified,
            )
            if triggered:
                self.interrupt_speech()
                return True
            return False

    async def run_voice_turn(
        self,
        audio: AsyncIterator[bytes],
        build_request: Callable[[str], GatewayRequest],
    ) -> VoiceTurnResult:
        if self._gateway is None:
            raise VoiceError("gateway_not_configured")
        if not self._stt.available:
            raise VoiceError("stt_not_configured")

        loop = asyncio.get_running_loop()
        with self._lock:
            self._interrupt_event = asyncio.Event()
            self._interrupt_loop = loop
            self._sink.reset()
            self._barge_in.reset()
            self._pipeline_playback_active = False
            self._emit_orb(OrbState.THINKING)
            interrupt_event = self._interrupt_event

        def on_first_audio_frame() -> None:
            with self._lock:
                self._pipeline_playback_active = True
                self._emit_orb(OrbState.SPEAKING)

        turn_sink = _TurnAudioSink(self._sink, on_first_audio_frame)
        try:
            result = await VoiceTurnPipeline(self._gateway).run(
                audio,
                self._stt,
                build_request,
                self._tts,
                turn_sink,
                interrupt_event,
            )
            return result
        finally:
            with self._lock:
                self._pipeline_playback_active = False
                self._interrupt_loop = None
                self._emit_orb(OrbState.IDLE)

    def orb_view(self, *, quiet_mode: bool = False) -> OrbView:
        with self._lock:
            now = self._now_ms()
            if self._last_orb_ms is not None and now < self._last_orb_ms:
                now = self._last_orb_ms
            focus = self._focus_probe()
            return self._orb.view(
                now_ms=now,
                owner_enabled=self._owner_opted_in,
                foreground=self._foreground,
                quiet_mode=quiet_mode,
                focus=focus,
            )
