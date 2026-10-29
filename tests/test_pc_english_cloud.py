"""English speech and real provider verification: no network or mic in CI."""
import io
import json
import urllib.error
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from max.config import Config
from max.pc.control import PCController
from max.pc import voice
from max.pc.cloud_stt import cloud_transcribe
from max.pc.web_app import PCBridge
from max.providers.base import ProviderAuthError, ProviderRateLimit


def wav_clip():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        f.writeframes(b"\x00\x00" * 1600)
    return buffer.getvalue()


def test_local_voice_forces_english_by_default(monkeypatch):
    seen = []
    monkeypatch.setenv("MAX_PC_STT_BACKEND", "local")
    monkeypatch.delenv("MAX_PC_STT_LANGUAGE", raising=False)
    monkeypatch.setattr(voice, "_cached_stt", lambda *args: (
        seen.append(args) or SimpleNamespace(
            transcribe_bytes=lambda payload: SimpleNamespace(text="Hi, Max!")
        )
    ))
    assert voice.transcribe(wav_clip()) == "Hi, Max!"
    assert seen[0][-1] == "en"


def test_local_voice_can_opt_into_auto_detection(monkeypatch):
    seen = []
    monkeypatch.setenv("MAX_PC_STT_BACKEND", "local")
    monkeypatch.setenv("MAX_PC_STT_LANGUAGE", "auto")
    monkeypatch.setattr(voice, "_cached_stt", lambda *args: (
        seen.append(args) or SimpleNamespace(
            transcribe_bytes=lambda payload: SimpleNamespace(text="Hi Max")
        )
    ))
    voice.transcribe(wav_clip())
    assert seen[0][-1] == ""


def test_cloud_transcription_is_opt_in_and_english_hinted():
    seen = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def read(self, n): return b'{"text":"Hi Max"}'
    def opener(req, timeout):
        seen.append((req, timeout))
        return Response()
    assert cloud_transcribe(wav_clip(), api_key="fake-key", opener=opener) == "Hi Max"
    req, timeout = seen[0]
    assert req.full_url == "https://api.openai.com/v1/audio/transcriptions"
    assert req.get_header("Authorization") == "Bearer fake-key"
    assert b'name="languages[]"\r\n\r\nen\r\n' in req.data
    assert b"gpt-transcribe" in req.data
    assert timeout == 25


def test_cloud_does_not_upload_without_separate_audio_key():
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected network call")
    with pytest.raises(Exception, match="OPENAI_API_KEY"):
        cloud_transcribe(wav_clip(), api_key="", opener=forbidden)


def test_cloud_classifies_auth_failure_without_echoing_secret():
    def failed(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://api.openai.com/v1/audio/transcriptions",
            401, "Invalid secret", {}, io.BytesIO(b'{"error":"secret-key"}'),
        )
    with pytest.raises(Exception) as exc:
        cloud_transcribe(wav_clip(), api_key="secret-key", opener=failed)
    assert "authentication failed" in str(exc.value).lower()
    assert "secret-key" not in str(exc.value)


def test_api_is_not_called_at_startup_and_success_is_verified(tmp_path):
    requested = []
    class FakeProvider:
        def complete(self, messages, tools=None):
            requested.append(messages)
            return SimpleNamespace(text="OK")
    agent = SimpleNamespace(
        provider=FakeProvider(),
        ask=lambda t, session_id: SimpleNamespace(text="response", ok=True),
    )
    cfg = Config(repo_root=tmp_path, data_dir=tmp_path,
                 provider="openai", base_url="https://example.com/v1", model="test")
    bridge = PCBridge(PCController(tmp_path), config=cfg, builder=lambda c: agent)
    assert bridge.state()["ready"] and not bridge.state()["model_verified"]
    assert requested == []  # constructing client did not verify reachability
    reply = bridge.test_ai_connection()
    assert reply["ok"] and requested
    assert bridge.state()["model_verified"]


@pytest.mark.parametrize("error,phrase", [
    (ProviderAuthError("bad key", code="http_401"), "authentication"),
    (ProviderRateLimit("quota", code="http_429"), "credits"),
])
def test_provider_failures_are_actionable_and_redacted(tmp_path, error, phrase):
    class FakeProvider:
        def complete(self, messages, tools=None): raise error
    agent = SimpleNamespace(provider=FakeProvider(), ask=lambda *a, **k: "")
    cfg = Config(repo_root=tmp_path, data_dir=tmp_path,
                 provider="openai", base_url="https://example.com/v1", model="test")
    bridge = PCBridge(PCController(tmp_path), config=cfg, builder=lambda c: agent)
    status = bridge.test_ai_connection()
    assert not status["ok"]
    assert phrase in status["message"].lower()
    assert not bridge.state()["model_verified"]
    assert "bad key" not in status["message"]


def test_ui_exposes_explicit_provider_test_and_separate_speech():
    root = Path(__file__).parents[1] / "src/max/pc/ui"
    for filename in ("companion.html", "index.html"):
        text = (root / filename).read_text(encoding="utf-8")
        assert "test_ai_connection" in text
        assert "gpt-transcribe" in text
    compact = (root / "companion.html").read_text(encoding="utf-8")
    for component in ("api-connect", "api-url", "api-model", "api-key"):
        assert 'id="' + component + '"' in compact
