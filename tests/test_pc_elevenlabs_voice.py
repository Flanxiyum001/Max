"""Permanent ElevenLabs voice ID, bounded synthesis, and honest playback errors."""
from __future__ import annotations

import io
import json
import wave
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from max.config import Config
from max.pc.control import PCController
from max.pc.elevenlabs_tts import (
    ElevenLabsSpeechError, VOICE_ID, _wav_from_pcm, speak, status, synthesize
)
from max.pc.voice import speak_max
from max.pc.web_app import PCBridge


@pytest.fixture(autouse=True)
def voice_config(monkeypatch):
    monkeypatch.delenv("MAX_PC_TTS_BACKEND", raising=False)
    monkeypatch.delenv("MAX_PC_TTS_VOICE_ID", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)


def test_exact_link_voice_is_permanent_default_without_any_credentials():
    info = status()
    assert info["backend"] == "elevenlabs"
    assert info["voice_id"] == "b5RPB35vTODb3BEmR3Fc"
    assert info["voice_id"] == VOICE_ID
    assert not info["key_configured"]
    assert not info["needs_cloud"] is False


def test_synthesis_sends_only_bounded_text_and_uses_fixed_https_endpoint():
    pcm = b"\x01\x02" * 24000
    seen = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, size):
            assert size == 6 * 1024 * 1024 + 1
            return pcm
    def open_req(req, timeout):
        seen.append((req, timeout))
        return Response()
    wav = synthesize(
        "Hello, boss!", api_key="example-private-key", opener=open_req,
    )
    with wave.open(io.BytesIO(wav), "rb") as f:
        assert f.getnchannels() == 1
        assert f.getframerate() == 24000
        assert f.getsampwidth() == 2
        assert f.readframes(24000) == pcm
    req, timeout = seen[0]
    assert req.full_url == (
        "https://api.elevenlabs.io/v1/text-to-speech/"
        "b5RPB35vTODb3BEmR3Fc?output_format=pcm_24000"
    )
    assert req.get_header("Xi-api-key") == "example-private-key"
    assert json.loads(req.data)["model_id"] == "eleven_flash_v2_5"
    assert timeout == 25


def test_default_does_not_silently_substitute_windows_voice(monkeypatch):
    import max.pc.elevenlabs_tts as mod
    requests = []
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda *args, **kwargs: requests.append(1))
    with pytest.raises(ElevenLabsSpeechError, match="ELEVENLABS_API_KEY"):
        speak_max("Hello")
    assert requests == []


def test_speak_plays_wav_after_successful_synthesis():
    captured = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, n): return b"\x00\x00" * 100
    assert speak("Hi Max", api_key="fake", opener=lambda *a, **k: Response(),
                 player=captured.append)
    assert len(captured) == 1 and captured[0][:4] == b"RIFF"


def test_no_voice_for_empty_or_invalid_pcm():
    assert synthesize("   ", api_key="any") == b""
    with pytest.raises(ElevenLabsSpeechError, match="invalid PCM"):
        _wav_from_pcm(b"\x01")


@pytest.mark.parametrize("code,part", [
    (401, "key or voice"),
    (403, "key or voice"),
    (404, "voice ID"),
    (429, "credits"),
    (500, "HTTP 500"),
])
def test_provider_errors_do_not_leak_credential_or_fallback(code, part):
    def denied(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://api.elevenlabs.io", code, "bad key", {},
            io.BytesIO(b"secret-shall-not-leak"),
        )
    with pytest.raises(ElevenLabsSpeechError) as err:
        synthesize("Hello", api_key="private-secret", opener=denied)
    assert part.lower() in str(err.value).lower()
    assert "private-secret" not in str(err.value)
    assert "secret-shall-not-leak" not in str(err.value)


def test_windows_voice_is_an_explicit_opt_in(monkeypatch):
    import max.pc.voice as mod
    monkeypatch.setenv("MAX_PC_TTS_BACKEND", "windows")
    captured = []
    monkeypatch.setattr(mod, "say", lambda text: captured.append(text) or True)
    assert mod.speak_max("Only when requested")
    assert captured == ["Only when requested"]


def test_bridge_exposes_selected_voice_and_test_reports_missing_key(tmp_path):
    bridge = PCBridge(
        PCController(tmp_path),
        config=Config(repo_root=tmp_path, data_dir=tmp_path, provider="mock"),
    )
    assert bridge.state()["tts"]["voice_id"] == VOICE_ID
    assert not bridge.state()["tts"]["key_configured"]
    assert not bridge._can_speak()
    result = bridge.test_voice()
    assert not result["ok"]
    assert "ELEVENLABS_API_KEY" in result["message"]
    assert bridge.state()["tts_error"] == result["message"]


def test_bridge_voice_test_succeeds_with_test_speaker_and_no_real_network(tmp_path):
    spoken = []
    bridge = PCBridge(
        PCController(tmp_path),
        config=Config(repo_root=tmp_path, data_dir=tmp_path),
        speaker=lambda words: spoken.append(words) or True,
    )
    result = bridge.test_voice()
    assert result["ok"]
    assert spoken == ["Hello. I'm Max, your personal assistant."]
    assert not bridge._speaking.is_set()


def test_voice_key_is_not_exposed_in_model_state_or_ui(monkeypatch, tmp_path):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "secret-that-must-not-leak")
    bridge = PCBridge(
        PCController(tmp_path), config=Config(repo_root=tmp_path, data_dir=tmp_path),
    )
    assert bridge.state()["tts"]["key_configured"]
    assert "secret-that-must-not-leak" not in str(bridge.state())
    path = Path(__file__).parents[1] / "src/max/pc/ui"
    for file in ("companion.html", "index.html"):
        html = (path / file).read_text(encoding="utf-8")
        assert "test_voice" in html
        assert "b5RPB35vTODb3BEmR3Fc" in html
        assert "secret-that-must-not-leak" not in html
