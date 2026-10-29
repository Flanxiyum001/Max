"""Optional local speech: press-to-talk microphone plus Windows male voice.

No passive microphone access. Audio and transcript aren't written to audit logs.
No download at boot. A faster-whisper model must be present locally, or the
user must deliberately opt in to its first-use download.
"""
from __future__ import annotations

import io
import os
import re
import wave
from functools import lru_cache

from ..speech import SttConfig, SttService


def say(text: str, *, engine_factory=None) -> bool:
    """Speak via the installed OS voice; return False if TTS is unavailable."""
    if engine_factory is None:
        try:
            import pyttsx3
        except ImportError:
            return False
        engine_factory = pyttsx3.init
    engine = engine_factory()
    try:
        voices = engine.getProperty("voices") or []
        males = [
            v for v in voices
            if any(term in (str(getattr(v, "name", "")) + " " +
                            str(getattr(v, "gender", ""))).lower()
                   for term in ("david", "male", "guy", "mark", "ryan"))
        ]
        if males:
            engine.setProperty("voice", males[0].id)
        engine.setProperty("rate", 175)
        # Never speak extremely large retrieved/web content.
        cleaned = re.sub(r"[\x00-\x1f]", " ", str(text))[:700]
        engine.say(cleaned)
        engine.runAndWait()
        return True
    finally:
        engine.stop()


def mic_wav(*, seconds: float = 4.5, sample_rate: int = 16000, recorder=None) -> bytes:
    """Capture only on explicit user interaction; 10-second hard upper bound."""
    if not 0.1 <= seconds <= 10:
        raise ValueError("recording duration must be 0.1–10 seconds")
    if recorder is None:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError("install the optional PC voice extra: pip install -e '.[pc]'") from exc
        def recorder(frames, samplerate):
            samples = sd.rec(frames, samplerate=samplerate, channels=1, dtype="int16")
            sd.wait()
            return samples.tobytes()
    frames = round(seconds * sample_rate)
    pcm = recorder(frames, sample_rate)
    if len(pcm) != frames * 2:
        raise ValueError("microphone produced unexpected PCM length")
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return out.getvalue()


@lru_cache(maxsize=4)
def _cached_stt(model_size: str, device: str, compute_type: str,
                allow_download: bool) -> SttService:
    """Reuse the same loaded Whisper model across voice chunks on this PC."""
    return SttService(
        SttConfig(
            backend="faster-whisper",
            model_size=model_size,
            device=device,
            compute_type=compute_type,
            local_files_only=not allow_download,
            max_duration_s=12.0,
        )
    )


_SAFE_MODELS = frozenset({"tiny", "tiny.en", "base", "base.en", "small", "small.en"})


def speech_model_name() -> str:
    """Only named, size-bounded local Whisper models are installable via the UI."""
    return os.environ.get("MAX_STT_MODEL_SIZE", "tiny").strip().lower()


@lru_cache(maxsize=8)
def speech_model_status(model_size: str | None = None) -> dict:
    """A local-cache-only check; never records audio or touches the network."""
    name = model_size or speech_model_name()
    result = {"model": name, "backend_installed": False, "cached": False}
    if name not in _SAFE_MODELS:
        result["error"] = "Use MAX_STT_MODEL_SIZE=tiny, base or small."
        return result
    try:
        from faster_whisper.utils import download_model
    except ImportError:
        result["error"] = "faster-whisper is not installed. Reinstall .[pc,stt,desktop]."
        return result
    result["backend_installed"] = True
    try:
        from pathlib import Path
        cache = Path(download_model(name, local_files_only=True))
        result["cached"] = (cache / "model.bin").is_file()
    except (OSError, RuntimeError, ValueError):
        result["cached"] = False
    return result


def install_speech_model(model_size: str | None = None, *, downloader=None) -> str:
    """Explicit opt-in download from Faster Whisper's published model registry.

    The call is only reachable via a deliberate INSTALL button, never at app
    startup or when merely pressing TALK. Normal transcription remains offline.
    """
    from pathlib import Path

    name = model_size or speech_model_name()
    if name not in _SAFE_MODELS:
        raise ValueError("Unsupported speech model. Choose tiny, base or small.")
    if downloader is None:
        try:
            from faster_whisper.utils import download_model
        except ImportError as exc:
            raise RuntimeError(
                "faster-whisper isn't installed. Run pip install -e '.[pc,stt,desktop]'"
            ) from exc
        downloader = download_model
    cache = Path(downloader(name, local_files_only=False))
    if not (cache / "model.bin").is_file():
        raise RuntimeError("Speech model download incomplete. Retry after checking network/storage.")
    speech_model_status.cache_clear()
    return f"Whisper {name} is cached locally. Try TALK or HANDS-FREE."


def speech_failure(exc: Exception) -> str:
    """Useful errors without showing file paths, credentials or stack traces."""
    from ..speech import SttError

    if isinstance(exc, SttError):
        detail = str(exc).lower()
        if "not installed" in detail or "not available" in detail:
            return "Faster Whisper isn't installed. Reinstall with .[pc,stt,desktop]."
        if "failed to load whisper model" in detail or "model" in detail and "load" in detail:
            return "Local Whisper model missing or unable to load. Open SPEECH SETUP and install the speech model; if already installed, verify CPU/CUDA settings."
        if "timed out" in detail:
            return "Speech recognition timed out. Try MAX_STT_MODEL_SIZE=tiny and MAX_STT_DEVICE=cpu."
        return "Speech recognition failed (" + type(exc).__name__ + "). Check speech model and audio format."
    if isinstance(exc, (OSError, RuntimeError)):
        return "Microphone unavailable. Check Windows Settings > Privacy & security > Microphone, your input device, and sounddevice installation."
    return "Speech input failed (" + type(exc).__name__ + "). Open SPEECH SETUP for diagnostics."


def transcribe(wav: bytes, *, service=None) -> str:
    if service is None:
        allow_download = os.environ.get("MAX_PC_STT_ALLOW_DOWNLOAD", "0") == "1"
        service = _cached_stt(
            os.environ.get("MAX_STT_MODEL_SIZE", "tiny"),
            os.environ.get("MAX_STT_DEVICE", "cpu"),
            os.environ.get("MAX_STT_COMPUTE_TYPE", "int8"),
            allow_download,
        )
    return service.transcribe_bytes(wav).text.strip()
