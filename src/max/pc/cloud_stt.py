"""Optional OpenAI cloud speech transcription, separate from MAX's LLM.

Requires explicit MAX_PC_STT_BACKEND=cloud and OPENAI_API_KEY. Audio leaves
the PC only in this opted-in mode. No tokens are stored or logged.
This is not a GPT-Live integration; it is the simpler recorded-audio route.
"""
from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request

from ..speech import SttError

_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
_MAX_AUDIO = 2 * 1024 * 1024


def cloud_transcribe(wav: bytes, *, api_key: str | None = None,
                     opener=None, timeout: float = 25) -> str:
    """Send bounded WAV to gpt-transcribe with English language hint."""
    if not isinstance(wav, bytes) or not wav.startswith(b"RIFF") or len(wav) < 44:
        raise SttError("Invalid microphone WAV recording")
    if len(wav) > _MAX_AUDIO:
        raise SttError("Audio clip too large for MAX's cloud speech limit")
    key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
    if not key or any(c in key for c in "\r\n"):
        raise SttError("Cloud speech requires a separate OPENAI_API_KEY")
    boundary = "max-audio-" + secrets.token_hex(16)
    parts = []
    for name, value in (
        ("model", "gpt-transcribe"),
        ("languages[]", "en"),
        ("keywords[]", "Max"),
        ("keywords[]", "Ollama"),
        ("keywords[]", "Valorant"),
        ("keywords[]", "Premiere"),
        ("response_format", "json"),
    ):
        parts.append(
            ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"" +
             name + "\"\r\n\r\n" + value + "\r\n").encode("utf-8")
        )
    parts.extend([
        ("--" + boundary + "\r\nContent-Disposition: form-data; name=\"file\"; "
         "filename=\"speech.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode(),
        wav,
        ("\r\n--" + boundary + "--\r\n").encode(),
    ])
    request = urllib.request.Request(
        _ENDPOINT, data=b"".join(parts), method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "multipart/form-data; boundary=" + boundary,
            "Accept": "application/json",
        },
    )
    try:
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            raw = response.read(128 * 1024)
    except urllib.error.HTTPError as exc:
        status = getattr(exc, "code", 0)
        if status in (401, 403):
            raise SttError("Cloud transcription authentication failed (401/403). Verify OPENAI_API_KEY.") from None
        if status in (402, 429):
            raise SttError("Cloud transcription credit/rate limit reached. Check audio API quota.") from None
        raise SttError("Cloud transcription HTTP error " + str(status)) from None
    except (TimeoutError, OSError, urllib.error.URLError):
        raise SttError("Cloud speech network error or timeout") from None
    try:
        data = json.loads(raw.decode("utf-8"))
        text = data["text"]
        if not isinstance(text, str) or len(text) > 4000:
            raise ValueError("unexpected text")
        return text.strip()
    except (UnicodeError, ValueError, KeyError, TypeError):
        raise SttError("Cloud speech returned an invalid transcript response") from None
