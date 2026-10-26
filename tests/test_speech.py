"""Speech-to-text adapter: limits, cleanup, lazy backend, no transcript logging."""

from __future__ import annotations

import struct

import pytest

from max.speech import (
    MockBackend,
    SttConfig,
    SttError,
    SttService,
    FasterWhisperBackend,
    wav_duration,
)


def make_wav(seconds: float = 1.0, rate: int = 16000) -> bytes:
    frames = int(seconds * rate)
    data = b"\x00\x00" * frames
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(data), b"WAVE", b"fmt ", 16,
        1, 1, rate, rate * 2, 2, 16, b"data", len(data),
    )
    return header + data


def test_wav_duration_parsed_from_header():
    assert abs(wav_duration(make_wav(2.0)) - 2.0) < 0.05
    assert wav_duration(b"not a wav") is None


def test_mock_backend_transcribes_deterministically():
    service = SttService(SttConfig(backend="mock"))
    service._backend = MockBackend(text="hello max")
    result = service.transcribe_bytes(make_wav(1.0))
    assert result.text == "hello max"
    assert result.backend == "mock"
    assert result.confidence is None
    assert result.latency_s >= 0


def test_input_limits():
    service = SttService(SttConfig(backend="mock", max_file_bytes=100))
    with pytest.raises(SttError, match="exceeds"):
        service.transcribe_bytes(make_wav(1.0))
    with pytest.raises(SttError, match="empty"):
        service.transcribe_bytes(b"")


def test_duration_limit_rejects_long_audio():
    service = SttService(SttConfig(backend="mock", max_duration_s=0.5))
    with pytest.raises(SttError, match="duration"):
        service.transcribe_bytes(make_wav(2.0))


def test_audio_temp_file_is_deleted_after_processing(tmp_path, monkeypatch):
    import tempfile

    created = []
    real = tempfile.NamedTemporaryFile

    def spy(*args, **kwargs):
        handle = real(*args, **kwargs)
        created.append(handle.name)
        return handle

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", spy)
    service = SttService(SttConfig(backend="mock"))
    service._backend = MockBackend(text="hi")
    service.transcribe_bytes(make_wav(0.2))
    assert created
    import pathlib

    for name in created:
        assert not pathlib.Path(name).exists(), "audio must not be retained"


def test_audit_records_metadata_but_never_transcript_text():
    entries = []

    class Audit:
        def record(self, event, **detail):
            entries.append((event, detail))
            return detail

    service = SttService(SttConfig(backend="mock"), audit=Audit())
    service._backend = MockBackend(text="TOP-SECRET TRANSCRIPT")
    service.transcribe_bytes(make_wav(0.5))
    assert entries and entries[0][0] == "stt_transcribed"
    detail = entries[0][1]
    assert "text" not in detail and "TOP-SECRET" not in str(detail)
    assert "audio_bytes" in detail and "latency_s" in detail


def test_unknown_backend_is_actionable():
    service = SttService(SttConfig(backend="whisper-local"))
    with pytest.raises(SttError, match="unknown speech backend"):
        service.transcribe_bytes(make_wav(0.1))


def test_faster_whisper_backend_reports_availability_honestly():
    ok, reason = FasterWhisperBackend.available()
    assert isinstance(ok, bool)
    if not ok:
        assert "not installed" in reason or "install" in reason.lower()
    # unavailable package must never raise at import/status time
    status = SttService(SttConfig(backend="faster-whisper")).status()
    assert status["available"] == ok
    assert status["loaded"] is False  # lazy: nothing loaded until first use


def test_faster_whisper_missing_backend_does_not_crash_text_chat():
    service = SttService(SttConfig(backend="faster-whisper"))
    try:
        import faster_whisper  # noqa: F401

        pytest.skip("faster-whisper installed; loading would need model files")
    except Exception:
        pass
    with pytest.raises(SttError, match="not installed"):
        service.transcribe_bytes(make_wav(0.1))


def test_concurrency_limit_is_enforced(monkeypatch):
    service = SttService(SttConfig(backend="mock", max_concurrency=1, timeout_s=0.2))
    service._backend = MockBackend(text="x")
    assert service._lock.acquire(blocking=False), "semaphore should start free"
    try:
        with pytest.raises(SttError, match="busy"):
            service.transcribe_bytes(make_wav(0.1))
    finally:
        service._lock.release()


def test_benchmark_reports_real_measurements_only():
    service = SttService(SttConfig(backend="mock"))
    service._backend = MockBackend(text="ok")
    report = service.benchmark([make_wav(0.1), make_wav(0.2)])
    assert report["ok"] == 2
    assert report["median_latency_s"] is not None
    bad = SttService(SttConfig(backend="mock", max_file_bytes=10)).benchmark([make_wav(1.0)])
    assert bad["ok"] == 0 and "error" in bad["rows"][0]
