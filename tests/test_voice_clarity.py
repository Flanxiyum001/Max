"""Tests for PCM16 voice clarity conditioning (high-pass, noise gate, normalization)."""

from __future__ import annotations

import array
import math
import sys

import pytest

from max_core.voice.desktop_stt import clean_pcm16
from max_core.voice.pipeline import VoiceError


def _pcm_from_floats(values: list[float]) -> bytes:
    arr = array.array("h", (max(-32768, min(32767, int(round(v)))) for v in values))
    if sys.byteorder != "little":
        arr.byteswap()
    return arr.tobytes()


def _floats_from_pcm(raw: bytes) -> list[int]:
    arr = array.array("h")
    arr.frombytes(raw)
    if sys.byteorder != "little":
        arr.byteswap()
    return list(arr)


def test_clean_pcm16_rejects_empty_and_invalid() -> None:
    with pytest.raises(VoiceError, match="empty_audio"):
        clean_pcm16(b"")
    with pytest.raises(VoiceError, match="invalid_audio_format"):
        clean_pcm16(b"\x00\x00" * 100, sample_rate=1000)


def test_clean_pcm16_attenuates_background_noise_and_normalizes_speech() -> None:
    sr = 16_000
    # 0.2 s quiet low-frequency hum + 0.2 s 1 kHz speech-band tone + 0.2 s quiet noise
    quiet = [40.0 * math.sin(2.0 * math.pi * 60.0 * i / sr) for i in range(int(sr * 0.2))]
    speech = [2500.0 * math.sin(2.0 * math.pi * 1000.0 * i / sr) for i in range(int(sr * 0.2))]
    raw = _pcm_from_floats(quiet + speech + quiet)

    cleaned = _floats_from_pcm(clean_pcm16(raw, sample_rate=sr, channels=1))
    assert len(cleaned) == len(quiet) * 2 + len(speech)

    speech_start = len(quiet)
    speech_end = speech_start + len(speech)
    speech_peak = max(abs(s) for s in cleaned[speech_start:speech_end])
    # Normalized near -3 dBFS (~23170) or capped at 8x (20000)
    assert speech_peak >= 18_000

    # Trailing quiet segment (after hangover) is strongly attenuated vs speech
    tail = cleaned[speech_end + int(sr * 0.1) :]
    tail_rms = math.sqrt(sum(s * s for s in tail) / len(tail))
    assert tail_rms < 150.0
