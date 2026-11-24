"""Regression coverage for opt-in recognition settings without network or microphone."""

import asyncio
import io
import wave

import httpx
import pytest

from max_core.voice.desktop_stt import (
    DesktopSpeechToText,
    desktop_stt_from_environment,
    vocabulary_prompt,
)
from max_core.voice.pipeline import VoiceError
from max_core.web.app import clean_transcript, stt_config


def test_vocabulary_is_bounded_and_deduplicated() -> None:
    assert vocabulary_prompt("MAX, Ryoko, MAX, , bad\nword") == "MAX, Ryoko"
    assert len(vocabulary_prompt(",".join(f"term{i}" for i in range(100)))) <= 300


def test_explicit_model_and_endpoint_settings() -> None:
    stt = desktop_stt_from_environment(
        env={
            "MAX_STT_ENDPOINT": "http://127.0.0.1:8000/v1/audio/transcriptions",
            "MAX_STT_MODEL": "whisper-large-v3",
            "MAX_STT_API_KEY": "local",
        }
    )
    assert stt.available
    assert stt.model == "whisper-large-v3"
    assert not stt.requires_remote_upload


def test_remote_consent_classification_is_unchanged() -> None:
    stt = desktop_stt_from_environment(
        env={
            "MAX_STT_ENDPOINT": "https://api.groq.com/openai/v1/audio/transcriptions",
            "MAX_STT_MODEL": "whisper-large-v3",
            "MAX_STT_API_KEY": "test",
        }
    )
    assert stt.requires_remote_upload


def test_invalid_recognition_settings_fail_closed() -> None:
    with pytest.raises(VoiceError, match="invalid_stt_language"):
        DesktopSpeechToText(language="en-IN")
    with pytest.raises(VoiceError, match="invalid_stt_audio_mode"):
        DesktopSpeechToText(audio_mode="unknown")


def test_raw_audio_and_spelling_hints_are_sent_without_rewriting() -> None:
    pcm = b"\x01\x00\x02\x00" * 100
    transcript = "MAX, no, wait — review PR 18 in Ryoko."

    def handle(request: httpx.Request) -> httpx.Response:
        body = request.read()
        assert b"gpt-4o-transcribe" in body
        assert b"MAX, Ryoko, GitHub" in body
        assert b'name="language"' not in body
        start = body.index(b"RIFF")
        with wave.open(io.BytesIO(body[start:]), "rb") as audio:
            assert audio.getframerate() == 16000
            assert audio.readframes(audio.getnframes()) == pcm
        return httpx.Response(200, json={"text": transcript})

    async def frames():
        yield pcm

    stt = DesktopSpeechToText(
        api_key="test",
        model="gpt-4o-transcribe",
        vocabulary="MAX, Ryoko, GitHub",
        audio_mode="raw",
        transport=httpx.MockTransport(handle),
    )
    assert asyncio.run(stt.recognize(frames())) == transcript


def test_web_verbatim_is_explicit_and_preserves_real_short_replies() -> None:
    env = {"MAX_STT_ENDPOINT": "https://example.test/v1/audio/transcriptions"}
    legacy = stt_config(env)
    precise = stt_config({**env, "MAX_STT_VERBATIM": "yes"})
    assert legacy is not None and not legacy.verbatim
    assert precise is not None and precise.verbatim
    assert clean_transcript("Thank you.") == ""
    assert clean_transcript("Thank you.", verbatim=True) == "Thank you."
    assert clean_transcript("No, wait (not PR 12).", verbatim=True) == "No, wait (not PR 12)."
