"""Desktop text-to-speech adapter backed by PySide6.QtTextToSpeech with silent fallback.

This module uses lazy dynamic imports and structural Protocols so ``mypy --strict``
passes both with PySide6 installed and in headless Core CI without PySide6.
No audio hardware or Qt objects are created on import or in offscreen mode.
"""

from __future__ import annotations

import importlib
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Protocol, cast

from max_core.persona import VoicePreference
from max_core.voice.pipeline import MAX_OUTPUT_FRAME_BYTES, VoiceError

_HEADLESS_QPA_PLATFORMS = frozenset({"offscreen", "minimal"})
_PREFERRED_ENGINES = ("winrt", "sapi", "")


class _QtLocaleProtocol(Protocol):
    def name(self) -> str: ...

    def bcp47Name(self) -> str: ...


class _QtVoiceProtocol(Protocol):
    def name(self) -> str: ...

    def locale(self) -> _QtLocaleProtocol: ...

    def gender(self) -> object: ...


class _QtTextToSpeechProtocol(Protocol):
    def engine(self) -> str: ...

    def availableVoices(self) -> Sequence[_QtVoiceProtocol]: ...

    def allVoices(self, locale: _QtLocaleProtocol) -> Sequence[_QtVoiceProtocol]: ...

    def setLocale(self, locale: _QtLocaleProtocol) -> None: ...

    def setVoice(self, voice: _QtVoiceProtocol) -> None: ...

    def say(self, text: str) -> None: ...

    def stop(self, boundaryHint: object = ...) -> None: ...

    def state(self) -> object: ...

    def errorReason(self) -> object: ...


class _QtAudioDeviceProtocol(Protocol):
    def isNull(self) -> bool: ...


def _is_headless_env() -> bool:
    platform = os.environ.get("QT_QPA_PLATFORM", "").strip().lower()
    return platform in _HEADLESS_QPA_PLATFORMS


def _normalize_locale_tag(tag: str) -> tuple[str, str]:
    cleaned = tag.strip().replace("_", "-").lower()
    if not cleaned:
        return ("", "")
    parts = [part for part in cleaned.split("-") if part]
    language = parts[0]
    region = parts[1] if len(parts) > 1 else ""
    return (language, region)


def _voice_locale_tag(voice: _QtVoiceProtocol) -> tuple[str, str]:
    locale = voice.locale()
    bcp47 = locale.bcp47Name()
    name = locale.name()
    lang_bcp, reg_bcp = _normalize_locale_tag(bcp47)
    lang_name, reg_name = _normalize_locale_tag(name)
    if reg_name and not reg_bcp:
        return (lang_name or lang_bcp, reg_name)
    if reg_bcp:
        return (lang_bcp, reg_bcp)
    return (lang_bcp or lang_name, reg_bcp or reg_name)


def _is_female_voice(voice: _QtVoiceProtocol, female_gender: object = None) -> bool:
    gender = voice.gender()
    if female_gender is not None and gender == female_gender:
        return True
    text = str(gender).strip().lower()
    return text == "female" or text.endswith(".female")


def select_qt_voice(
    voices: Sequence[_QtVoiceProtocol],
    preference: VoicePreference,
    *,
    female_gender: object = None,
) -> _QtVoiceProtocol | None:
    """Select the best available voice matching ``VoicePreference``."""
    if not voices:
        return None

    if preference.selected and preference.voice_id is not None:
        for voice in voices:
            if voice.name() == preference.voice_id:
                return voice

    target_lang, target_region = _normalize_locale_tag(preference.locale)

    def rank(voice: _QtVoiceProtocol) -> tuple[int, int]:
        v_lang, v_region = _voice_locale_tag(voice)
        is_female = _is_female_voice(voice, female_gender=female_gender)
        exact_locale = (
            bool(target_lang)
            and v_lang == target_lang
            and (not target_region or v_region == target_region)
        )
        same_lang = bool(target_lang) and v_lang == target_lang

        if exact_locale and is_female:
            tier = 0
        elif same_lang and is_female:
            tier = 1
        elif exact_locale:
            tier = 2
        elif same_lang:
            tier = 3
        elif is_female:
            tier = 4
        else:
            tier = 5
        return (tier, 0)

    return min(voices, key=rank)


def _chunk_utf8_text(text: str, max_bytes: int = MAX_OUTPUT_FRAME_BYTES) -> list[bytes]:
    """Encode text into non-empty UTF-8 frames without splitting codepoints."""
    stripped = text.strip()
    if not stripped:
        return []
    encoded = stripped.encode("utf-8")
    if len(encoded) <= max_bytes:
        return [encoded]

    frames: list[bytes] = []
    current_chars: list[str] = []
    current_bytes = 0
    for ch in stripped:
        ch_bytes = len(ch.encode("utf-8"))
        if current_chars and current_bytes + ch_bytes > max_bytes:
            frames.append("".join(current_chars).encode("utf-8"))
            current_chars = [ch]
            current_bytes = ch_bytes
        else:
            current_chars.append(ch)
            current_bytes += ch_bytes
    if current_chars:
        frames.append("".join(current_chars).encode("utf-8"))
    return frames


class DesktopTextToSpeech:
    """Concrete desktop ``TextToSpeech`` adapter with graceful headless fallback."""

    def __init__(
        self,
        *,
        preference: VoicePreference | None = None,
        headless: bool | None = None,
        engine_factory: Callable[[str | None], _QtTextToSpeechProtocol] | None = None,
        available_engines_fn: Callable[[], Sequence[str]] | None = None,
        audio_outputs_fn: Callable[[], Sequence[_QtAudioDeviceProtocol]] | None = None,
    ) -> None:
        self._preference = preference if preference is not None else VoicePreference()
        if not isinstance(self._preference, VoicePreference):
            raise VoiceError("invalid_voice_preference")

        self._headless = _is_headless_env() if headless is None else bool(headless)
        self._lock = threading.RLock()
        self._driver: _QtTextToSpeechProtocol | None = None
        self._available = False
        self._engine_name = "mock" if self._headless else "none"
        self._reason: str | None = "headless_offscreen" if self._headless else None
        self._selected_voice_name: str | None = None
        self._speaking = False

        self._state_speaking: object = None
        self._state_ready: object = None
        self._state_error: object = None
        self._no_error: object = None
        self._immediate_hint: object = None
        self._female_gender: object = None
        self._c_locale: _QtLocaleProtocol | None = None
        self._preferred_locale: _QtLocaleProtocol | None = None

        if self._headless:
            return

        self._initialize_qt_tts(
            engine_factory=engine_factory,
            available_engines_fn=available_engines_fn,
            audio_outputs_fn=audio_outputs_fn,
        )

    def _initialize_qt_tts(
        self,
        *,
        engine_factory: Callable[[str | None], _QtTextToSpeechProtocol] | None,
        available_engines_fn: Callable[[], Sequence[str]] | None,
        audio_outputs_fn: Callable[[], Sequence[_QtAudioDeviceProtocol]] | None,
    ) -> None:
        qt_tts_mod: object | None = None
        qt_core_mod: object | None = None
        qt_mm_mod: object | None = None
        all_injected = (
            engine_factory is not None
            and available_engines_fn is not None
            and audio_outputs_fn is not None
        )

        try:
            qt_tts_mod = importlib.import_module("PySide6.QtTextToSpeech")
            qt_core_mod = importlib.import_module("PySide6.QtCore")
            qt_mm_mod = importlib.import_module("PySide6.QtMultimedia")
        except ModuleNotFoundError:
            if not all_injected:
                self._available = False
                self._engine_name = "none"
                self._reason = "pyside6_unavailable"
                return
        except Exception:
            if not all_injected:
                self._available = False
                self._engine_name = "none"
                self._reason = "tts_init_failed"
                return

        if qt_tts_mod is not None:
            tts_cls = getattr(qt_tts_mod, "QTextToSpeech", None)
            voice_cls = getattr(qt_tts_mod, "QVoice", None)
            if tts_cls is not None:
                state_enum = getattr(tts_cls, "State", None)
                err_enum = getattr(tts_cls, "ErrorReason", None)
                hint_enum = getattr(tts_cls, "BoundaryHint", None)
                self._state_speaking = getattr(state_enum, "Speaking", None)
                self._state_ready = getattr(state_enum, "Ready", None)
                self._state_error = getattr(state_enum, "Error", None)
                self._no_error = getattr(err_enum, "NoError", None)
                self._immediate_hint = getattr(hint_enum, "Immediate", None)
            if voice_cls is not None:
                gender_enum = getattr(voice_cls, "Gender", None)
                self._female_gender = getattr(gender_enum, "Female", None)

        if qt_core_mod is not None:
            locale_cls = getattr(qt_core_mod, "QLocale", None)
            if locale_cls is not None:
                c_fn = getattr(locale_cls, "c", None)
                if callable(c_fn):
                    self._c_locale = cast("_QtLocaleProtocol", c_fn())
                self._preferred_locale = cast(
                    "_QtLocaleProtocol", locale_cls(self._preference.locale)
                )

        outputs_probe = audio_outputs_fn
        if outputs_probe is None and qt_mm_mod is not None:
            media_devices_cls = getattr(qt_mm_mod, "QMediaDevices", None)
            if media_devices_cls is not None:
                raw_outputs_fn = getattr(media_devices_cls, "audioOutputs", None)
                if callable(raw_outputs_fn):
                    outputs_probe = cast(
                        "Callable[[], Sequence[_QtAudioDeviceProtocol]]",
                        raw_outputs_fn,
                    )

        if outputs_probe is not None:
            try:
                outputs = list(outputs_probe())
            except Exception:
                outputs = []
            valid_outputs = [
                dev for dev in outputs if not (hasattr(dev, "isNull") and dev.isNull())
            ]
            if not valid_outputs:
                self._available = False
                self._engine_name = "mock"
                self._reason = "no_audio_outputs"
                return

        engines_probe = available_engines_fn
        if engines_probe is None and qt_tts_mod is not None:
            tts_cls = getattr(qt_tts_mod, "QTextToSpeech", None)
            if tts_cls is not None:
                raw_engines_fn = getattr(tts_cls, "availableEngines", None)
                if callable(raw_engines_fn):
                    engines_probe = cast("Callable[[], Sequence[str]]", raw_engines_fn)

        available_engines: list[str] = []
        if engines_probe is not None:
            try:
                available_engines = list(engines_probe())
            except Exception:
                self._available = False
                self._engine_name = "mock"
                self._reason = "tts_init_failed"
                return
        candidates: list[str | None] = []
        for candidate in _PREFERRED_ENGINES:
            if candidate == "" or not available_engines or candidate in available_engines:
                candidates.append(candidate)
        if not candidates:
            candidates.append(None)

        factory = engine_factory
        if factory is None and qt_tts_mod is not None:
            tts_cls = getattr(qt_tts_mod, "QTextToSpeech", None)
            if callable(tts_cls):

                def _qt_factory(engine_name: str | None) -> _QtTextToSpeechProtocol:
                    if engine_name is None or engine_name == "":
                        return cast("_QtTextToSpeechProtocol", tts_cls())
                    return cast("_QtTextToSpeechProtocol", tts_cls(engine_name))

                factory = _qt_factory

        if factory is None:
            self._available = False
            self._engine_name = "none"
            self._reason = "tts_init_failed"
            return

        for engine_candidate in candidates:
            try:
                driver = factory(engine_candidate)
                state = driver.state()
                err = driver.errorReason()
                if self._is_driver_error(state, err):
                    continue
                actual_engine = (
                    str(driver.engine() or engine_candidate or "default").strip().lower()
                )
            except Exception:
                continue
            if actual_engine == "mock":
                continue
            self._driver = driver
            self._available = True
            self._engine_name = actual_engine
            self._reason = None
            self._configure_voice(driver)
            return

        self._available = False
        self._engine_name = "mock"
        self._reason = "tts_init_failed"

    def _is_driver_error(self, state: object, err: object) -> bool:
        if self._state_error is not None and state == self._state_error:
            return True
        if isinstance(state, str) and state.strip().lower() in {"error", "state.error"}:
            return True
        if getattr(state, "name", None) == "Error":
            return True

        if err is None:
            return False
        if self._no_error is not None and err == self._no_error:
            return False
        if type(err) is int:
            return err != 0
        if isinstance(err, str):
            return err.strip().lower() not in {
                "",
                "0",
                "noerror",
                "no_error",
                "errorreason.noerror",
                "none",
            }
        err_name = getattr(err, "name", None)
        if isinstance(err_name, str):
            return err_name != "NoError"
        if self._no_error is not None:
            return bool(err != self._no_error)
        return False

    def _configure_voice(self, driver: _QtTextToSpeechProtocol) -> None:
        try:
            voices = list(driver.availableVoices())
        except Exception:
            voices = []
        if not voices and self._c_locale is not None:
            try:
                voices = list(driver.allVoices(self._c_locale))
            except Exception:
                voices = []
        if not voices and self._preferred_locale is not None:
            try:
                voices = list(driver.allVoices(self._preferred_locale))
            except Exception:
                voices = []
        chosen = select_qt_voice(voices, self._preference, female_gender=self._female_gender)
        if chosen is None:
            return
        try:
            driver.setLocale(chosen.locale())
        except Exception:
            pass
        try:
            driver.setVoice(chosen)
            self._selected_voice_name = chosen.name()
        except Exception:
            self._selected_voice_name = None

    @property
    def available(self) -> bool:
        return self._available

    @property
    def engine_name(self) -> str:
        return self._engine_name

    @property
    def headless(self) -> bool:
        return self._headless

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def preference(self) -> VoicePreference:
        return self._preference

    @property
    def selected_voice_name(self) -> str | None:
        return self._selected_voice_name

    def is_speaking(self) -> bool:
        with self._lock:
            if self._driver is None:
                return False
            try:
                state = self._driver.state()
            except Exception:
                return self._speaking
            if self._state_speaking is not None and state == self._state_speaking:
                return True
            if self._state_ready is not None and state == self._state_ready:
                self._speaking = False
                return False
            if isinstance(state, str):
                lowered = state.strip().lower()
                if lowered in {"speaking", "state.speaking"}:
                    return True
                if lowered in {"ready", "state.ready"}:
                    self._speaking = False
                    return False
            state_name = getattr(state, "name", None)
            if state_name == "Speaking":
                return True
            if state_name == "Ready":
                self._speaking = False
                return False
            return self._speaking

    def speak(self, text: str) -> bool:
        if not isinstance(text, str):
            return False
        cleaned = text.strip()
        if not cleaned:
            return False
        with self._lock:
            if not self._available or self._driver is None:
                return False
            try:
                self._speaking = True
                self._driver.say(cleaned)
                return True
            except Exception:
                self._speaking = False
                return False

    def stop(self) -> None:
        with self._lock:
            self._speaking = False
            if self._driver is None:
                return
            try:
                if self._immediate_hint is not None:
                    self._driver.stop(self._immediate_hint)
                else:
                    self._driver.stop()
            except Exception:
                pass

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield bounded UTF-8 chunks suitable for ``DesktopAudioSink``."""
        if not isinstance(text, str):
            return
        for frame in _chunk_utf8_text(text):
            yield frame


class DesktopAudioSink:
    """``AudioSink`` adapter bridging ``VoiceTurnPipeline`` frames to ``DesktopTextToSpeech``."""

    def __init__(
        self,
        tts: DesktopTextToSpeech,
        *,
        on_play: Callable[[bytes], Awaitable[None]] | None = None,
        on_stop: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._tts = tts
        self._on_play = on_play
        self._on_stop = on_stop
        self._stopped = False

    def reset(self) -> None:
        self._stopped = False

    async def play(self, frame: bytes) -> None:
        if not isinstance(frame, bytes) or not frame or len(frame) > MAX_OUTPUT_FRAME_BYTES:
            raise VoiceError("invalid_speech_frame")
        if self._stopped:
            return
        if self._on_play is not None:
            await self._on_play(frame)
            return
        text = frame.decode("utf-8", errors="replace").strip()
        if text:
            self._tts.speak(text)

    async def stop(self) -> None:
        self._stopped = True
        self._tts.stop()
        if self._on_stop is not None:
            await self._on_stop()
