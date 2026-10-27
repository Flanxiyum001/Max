"""Speech-to-text adapter (optional, transport-independent).

Two backends behind one interface:

* ``mock``  — deterministic, zero-dependency, used by tests and by default
  so text chat never depends on a model download.
* ``faster-whisper`` — lazily imported and lazily loaded. Nothing is
  downloaded at startup; if the package or the model files are missing the
  caller gets an actionable error while Discord text chat keeps running.

Privacy: input audio is bounded (size + duration), written to a temporary
file that is always deleted, and neither raw audio nor transcript text is
logged by default — only backend, byte count, duration and latency.
"""

from __future__ import annotations

import os
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


class SttError(Exception):
    """Recoverable speech-to-text failure (missing backend, bad input, timeout)."""


@dataclass
class SttConfig:
    backend: str = "mock"  # mock | faster-whisper
    model_size: str = "tiny"  # tiny | base | small | ...
    compute_type: str = "int8"
    device: str = "cpu"
    language: str | None = None
    timeout_s: float = 30.0
    max_file_bytes: int = 10 * 1024 * 1024
    max_duration_s: float = 300.0
    max_concurrency: int = 1
    model_dir: str = ""  # HF cache / model download root (never auto-created)
    local_files_only: bool = True  # no automatic downloads during bot startup

    def redacted(self) -> dict:
        return {
            "backend": self.backend,
            "model_size": self.model_size,
            "compute_type": self.compute_type,
            "device": self.device,
            "language": self.language,
            "timeout_s": self.timeout_s,
            "max_file_bytes": self.max_file_bytes,
            "max_duration_s": self.max_duration_s,
            "max_concurrency": self.max_concurrency,
            "model_dir": self.model_dir,
            "local_files_only": self.local_files_only,
        }


@dataclass
class Transcription:
    text: str
    backend: str
    audio_bytes: int
    audio_duration_s: float | None
    latency_s: float
    confidence: float | None = None  # only when the backend really reports one
    language: str | None = None
    error: str | None = None

    def summary(self) -> str:
        """Log-safe summary: never contains transcript text."""
        return (
            f"backend={self.backend} bytes={self.audio_bytes} "
            f"duration={self.audio_duration_s} latency={round(self.latency_s, 3)}s"
        )


def wav_duration(data: bytes) -> float | None:
    """Duration of a PCM WAV file from its header; None if not a WAV."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    pos, fmt, rate, channels, bits = 12, None, None, None, None
    while pos + 8 <= len(data):
        chunk_id = data[pos : pos + 4]
        size = struct.unpack("<I", data[pos + 4 : pos + 8])[0]
        body = data[pos + 8 : pos + 8 + size]
        if chunk_id == b"fmt " and len(body) >= 16:
            fmt, channels, rate, _byte_rate = struct.unpack("<HHII", body[:12])
            bits = struct.unpack("<H", body[14:16])[0]
        elif chunk_id == b"data" and fmt == 1 and rate and channels and bits:
            frame = channels * bits // 8
            return round(size / (rate * frame), 3) if frame else None
        pos += 8 + size + (size % 2)
    return None


class MockBackend:
    """Deterministic backend for tests and offline operation."""

    name = "mock"

    def __init__(self, text: str = ""):
        self.text = text

    def transcribe(self, path: Path, cfg: SttConfig) -> tuple[str, float | None]:
        return self.text, None


class FasterWhisperBackend:
    """Lazy wrapper: imports and loads only on first use."""

    name = "faster-whisper"

    def __init__(self, cfg: SttConfig):
        self.cfg = cfg
        self._model = None

    @staticmethod
    def available() -> tuple[bool, str]:
        try:
            import faster_whisper  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            return False, (
                "faster-whisper is not installed — run "
                "`uv pip install -e '.[stt]'` (optional extra) to enable local transcription"
                + (f" ({exc.__class__.__name__})" if str(exc) else "")
            )
        return True, "available"

    def _load(self):
        if self._model is None:
            ok, reason = self.available()
            if not ok:
                raise SttError(reason)
            from faster_whisper import WhisperModel

            kwargs = {
                "device": self.cfg.device,
                "compute_type": self.cfg.compute_type,
            }
            try:
                self._model = WhisperModel(
                    self.cfg.model_size,
                    download_root=self.cfg.model_dir or None,
                    local_files_only=self.cfg.local_files_only,
                    **kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                raise SttError(
                    f"failed to load whisper model '{self.cfg.model_size}': {exc.__class__.__name__}. "
                    "Pre-download it with `huggingface-cli download` or set MAX_STT_LOCAL_FILES_ONLY=0 "
                    "once, deliberately, outside normal startup."
                ) from None
        return self._model

    def transcribe(self, path: Path, cfg: SttConfig) -> tuple[str, float | None]:
        model = self._load()
        segments, info = model.transcribe(
            str(path),
            language=cfg.language,
            beam_size=1,
            vad_filter=True,
        )
        parts = []
        confidence = None
        for segment in segments:
            parts.append(segment.text.strip())
            if getattr(segment, "avg_logprob", None) is not None:
                # avg_logprob <= 0; map to a 0..1-ish score only if present.
                confidence = max(0.0, min(1.0, 1.0 + segment.avg_logprob / 10.0))
        lang = getattr(info, "language", None)
        self._last_language = lang
        return " ".join(p for p in parts if p).strip(), confidence


class SttService:
    """Validates input, bounds resources, manages the backend lifecycle."""

    def __init__(self, config: SttConfig | None = None, audit=None):
        self.config = config or SttConfig()
        self.audit = audit
        self._backend = None
        self._lock = threading.Semaphore(max(1, self.config.max_concurrency))

    # ------------------------------------------------------------ lifecycle
    @property
    def enabled(self) -> bool:
        return self.config.backend in {"mock", "faster-whisper"}

    def status(self) -> dict:
        info = {"configured": self.config.redacted(), "loaded": self._backend is not None}
        if self.config.backend == "faster-whisper":
            ok, reason = FasterWhisperBackend.available()
            info["available"] = ok
            info["diagnostic"] = reason
        elif self.config.backend == "mock":
            info["available"] = True
        else:
            info["available"] = False
            info["diagnostic"] = f"unknown speech backend '{self.config.backend}'"
        return info

    def _get_backend(self):
        if self._backend is None:
            if self.config.backend == "mock":
                self._backend = MockBackend()
            elif self.config.backend == "faster-whisper":
                self._backend = FasterWhisperBackend(self.config)  # loads lazily
            else:
                raise SttError(
                    f"unknown speech backend '{self.config.backend}' (expected mock or faster-whisper)"
                )
        return self._backend

    # ----------------------------------------------------------- transcription
    def transcribe_bytes(self, data: bytes) -> Transcription:
        cfg = self.config
        if not isinstance(data, (bytes, bytearray)):
            raise SttError("audio input must be bytes")
        if len(data) == 0:
            raise SttError("empty audio input")
        if len(data) > cfg.max_file_bytes:
            raise SttError(
                f"audio of {len(data)} bytes exceeds the {cfg.max_file_bytes} byte limit"
            )
        duration = wav_duration(bytes(data))
        if duration is not None and duration > cfg.max_duration_s:
            raise SttError(
                f"audio duration {duration}s exceeds the {cfg.max_duration_s}s limit"
            )

        started = time.monotonic()
        tmp_path = None
        if not self._lock.acquire(timeout=cfg.timeout_s):
            raise SttError("speech backend is busy (concurrency limit reached)")
        try:
            with tempfile.NamedTemporaryFile(
                suffix=".wav", prefix="max-stt-", delete=False, dir=None
            ) as handle:
                handle.write(data)
                tmp_path = Path(handle.name)
            backend = self._get_backend()
            text, confidence = _with_timeout(
                lambda: backend.transcribe(tmp_path, cfg), cfg.timeout_s
            )
            result = Transcription(
                text=text,
                backend=backend.name,
                audio_bytes=len(data),
                audio_duration_s=duration,
                latency_s=round(time.monotonic() - started, 3),
                confidence=confidence,
                language=getattr(backend, "_last_language", None),
            )
            if self.audit is not None:
                # Bounded, non-content audit entry (no transcript, no audio).
                self.audit.record("stt_transcribed", **_audit_fields(result))
            return result
        finally:
            self._lock.release()
            if tmp_path is not None:
                try:
                    tmp_path.unlink()  # audio is never retained by default
                except OSError:
                    pass

    def benchmark(self, samples: list[bytes]) -> dict:
        """Measure latency on provided fixtures. Never fabricates results."""
        rows = []
        for data in samples:
            try:
                result = self.transcribe_bytes(data)
                rows.append(
                    {
                        "bytes": result.audio_bytes,
                        "duration_s": result.audio_duration_s,
                        "latency_s": result.latency_s,
                        "backend": result.backend,
                        "chars": len(result.text),
                    }
                )
            except SttError as exc:
                rows.append({"bytes": len(data), "error": str(exc)[:200]})
        latencies = sorted(r["latency_s"] for r in rows if "latency_s" in r)
        return {
            "backend": self.config.backend,
            "samples": len(samples),
            "ok": len(latencies),
            "median_latency_s": latencies[len(latencies) // 2] if latencies else None,
            "rows": rows,
        }


def _audit_fields(result: Transcription) -> dict:
    return {
        "backend": result.backend,
        "audio_bytes": result.audio_bytes,
        "audio_duration_s": result.audio_duration_s,
        "latency_s": result.latency_s,
        "chars": len(result.text),
    }


def _with_timeout(fn, timeout_s: float):
    """Run transcription with a hard timeout; audio is deleted by the caller."""
    outcome: dict = {}

    def runner():
        try:
            outcome["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise SttError(f"transcription timed out after {timeout_s}s")
    if "error" in outcome:
        err = outcome["error"]
        if isinstance(err, SttError):
            raise err
        raise SttError(f"transcription failed: {err.__class__.__name__}")
    return outcome["value"]
