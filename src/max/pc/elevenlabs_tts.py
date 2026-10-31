"""ElevenLabs voice output for MAX's local Windows desktop assistant.

The selected voice ID is a persisted source-code default, not a downloaded
voice. The actual audio is synthesized by ElevenLabs on each spoken reply.
Only text explicitly chosen for speech is sent to ElevenLabs. Credentials
remain in the local ELEVENLABS_API_KEY environment variable.
"""
from __future__ import annotations

import io
import json
import os
import re
import urllib.error
import urllib.request
import wave

VOICE_ID = "b5RPB35vTODb3BEmR3Fc"
MODEL_ID = "eleven_flash_v2_5"
SAMPLE_RATE = 24000
_MAX_AUDIO_BYTES = 6 * 1024 * 1024
_ENDPOINT = "https://api.elevenlabs.io/v1/text-to-speech/"


class ElevenLabsSpeechError(Exception):
    """Safe, user-visible speech error (never contains credentials)."""


def status() -> dict:
    backend = os.environ.get("MAX_PC_TTS_BACKEND", "elevenlabs").strip().lower()
    if backend not in {"elevenlabs", "windows"}:
        backend = "elevenlabs"
    return {
        "backend": backend,
        "voice_id": os.environ.get("MAX_PC_TTS_VOICE_ID", VOICE_ID) if backend == "elevenlabs" else None,
        "model_id": MODEL_ID if backend == "elevenlabs" else None,
        "key_configured": bool(os.environ.get("ELEVENLABS_API_KEY", "").strip()) if backend == "elevenlabs" else True,
        "needs_cloud": backend == "elevenlabs",
    }


def _wav_from_pcm(pcm: bytes) -> bytes:
    if not pcm or len(pcm) % 2:
        raise ElevenLabsSpeechError("ElevenLabs returned invalid PCM audio.")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SAMPLE_RATE)
        audio.writeframes(pcm)
    return buffer.getvalue()


def synthesize(text: str, *, opener=None, api_key: str | None = None,
               voice_id: str | None = None) -> bytes:
    """Return a WAV for Windows playback, with bounded remote PCM generation.

    The request uses no local LLM credits. It consumes ElevenLabs TTS quota.
    In particular, the ElevenLabs voice is NOT downloadable as local weights.
    """
    key = os.environ.get("ELEVENLABS_API_KEY", "") if api_key is None else api_key
    if not isinstance(key, str) or not key.strip() or "\n" in key or "\r" in key:
        raise ElevenLabsSpeechError(
            "ElevenLabs voice selected, but ELEVENLABS_API_KEY is missing. "
            "Add it to the private .env.local on your PC; no other voice was substituted."
        )
    selected = voice_id or os.environ.get("MAX_PC_TTS_VOICE_ID", VOICE_ID)
    if not re.fullmatch(r"[A-Za-z0-9]{20}", selected):
        raise ElevenLabsSpeechError("Invalid ElevenLabs voice ID.")
    cleaned = re.sub(r"[\x00-\x1f]+", " ", str(text)).strip()[:700]
    if not cleaned:
        return b""
    request = urllib.request.Request(
        _ENDPOINT + selected + "?output_format=pcm_24000",
        data=json.dumps({
            "text": cleaned,
            "model_id": MODEL_ID,
        }).encode("utf-8"),
        method="POST",
        headers={
            "xi-api-key": key,
            "Content-Type": "application/json",
            "Accept": "audio/pcm",
            "User-Agent": "max-assistant/1.0",
        },
    )
    try:
        with (opener or urllib.request.urlopen)(request, timeout=25) as res:
            pcm = res.read(_MAX_AUDIO_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status_code = getattr(exc, "code", 0)
        if status_code in (401, 403):
            raise ElevenLabsSpeechError(
                "ElevenLabs denied this key or voice (401/403). "
                "Check the key, account plan and whether the voice is in My Voices."
            ) from None
        if status_code == 404:
            raise ElevenLabsSpeechError(
                "ElevenLabs couldn't find this voice ID (404). Check access to the shared voice."
            ) from None
        if status_code == 429:
            raise ElevenLabsSpeechError(
                "ElevenLabs TTS credits or rate limit reached (429). "
                "The selected voice was not replaced."
            ) from None
        raise ElevenLabsSpeechError(
            f"ElevenLabs synthesis failed (HTTP {status_code}). The selected voice was not replaced."
        ) from None
    except (TimeoutError, OSError, urllib.error.URLError):
        raise ElevenLabsSpeechError("ElevenLabs voice request timed out or could not connect.") from None
    if len(pcm) > _MAX_AUDIO_BYTES:
        raise ElevenLabsSpeechError("ElevenLabs response exceeded MAX's audio safety limit.")
    return _wav_from_pcm(pcm)


def speak(text: str, *, api_key: str | None = None, opener=None, player=None) -> bool:
    audio = synthesize(text, api_key=api_key, opener=opener)
    if not audio:
        return False
    if player is None:
        try:
            import winsound
        except ImportError:
            raise ElevenLabsSpeechError("ElevenLabs playback requires the Windows desktop.") from None
        player = lambda data: winsound.PlaySound(data, winsound.SND_MEMORY)
    try:
        player(audio)
    except Exception:
        raise ElevenLabsSpeechError("Windows couldn't play the ElevenLabs audio.") from None
    return True
