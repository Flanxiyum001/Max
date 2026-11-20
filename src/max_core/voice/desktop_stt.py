"""Desktop speech-to-text adapter and opt-in push-to-talk Qt microphone recorder.

Microphone recording is strictly opt-in and never starts on import or initialization.
Audio buffers are bounded (8 MB total, 256 KB frames) and zeroed/cleared immediately
after transcription or cancellation. OpenAI ``/v1/audio/transcriptions`` requests use
an injectable ``httpx.AsyncClient`` / transport with fail-closed URL validation and
sanitized ``VoiceError`` codes.
"""

from __future__ import annotations

import importlib
import inspect
import io
import ipaddress
import json
import math
import os
import threading
import wave
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from max_core.local_config import DesktopConfig, ProviderConfig
from max_core.voice.pipeline import (
    MAX_INPUT_AUDIO_BYTES,
    MAX_INPUT_FRAME_BYTES,
    MAX_TRANSCRIPT_CHARS,
    VoiceError,
    bounded_microphone,
)

DEFAULT_STT_ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"
DEFAULT_STT_MODEL = "whisper-1"
DEFAULT_SAMPLE_RATE = 16_000
DEFAULT_CHANNELS = 1
MAX_STT_RESPONSE_BYTES = 1_000_000

_HEADLESS_QPA_PLATFORMS = frozenset({"offscreen", "minimal"})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _is_headless_env() -> bool:
    platform = os.environ.get("QT_QPA_PLATFORM", "").strip().lower()
    return platform in _HEADLESS_QPA_PLATFORMS


def _is_loopback_host(hostname: str) -> bool:
    lowered = hostname.strip().lower()
    if lowered in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def _validate_stt_url(raw_url: str) -> tuple[str, str, str]:
    if (
        not isinstance(raw_url, str)
        or not raw_url
        or raw_url != raw_url.strip()
        or _has_control_chars(raw_url)
        or any(ch.isspace() for ch in raw_url)
        or "\\" in raw_url
    ):
        raise VoiceError("invalid_stt_endpoint")
    try:
        parts = urlsplit(raw_url)
        port = parts.port
    except ValueError:
        raise VoiceError("invalid_stt_endpoint") from None

    scheme = parts.scheme.lower()
    hostname = parts.hostname or ""
    if (
        scheme not in {"https", "http"}
        or not hostname
        or (port is not None and port == 0)
        or parts.username is not None
        or parts.password is not None
        or "@" in parts.netloc
        or parts.query != ""
        or parts.fragment != ""
    ):
        raise VoiceError("invalid_stt_endpoint")
    if scheme == "http" and not _is_loopback_host(hostname):
        raise VoiceError("invalid_stt_endpoint")
    return (scheme, parts.netloc, parts.path or "")


def derive_transcription_endpoint(endpoint: str) -> str:
    """Derive a validated ``/v1/audio/transcriptions`` URL from a provider endpoint."""
    scheme, netloc, path = _validate_stt_url(endpoint)
    if path.endswith("/chat/completions"):
        new_path = path[: -len("/chat/completions")] + "/audio/transcriptions"
    elif path.endswith("/audio/transcriptions"):
        new_path = path
    elif path.rstrip("/") == "/v1" or path in {"", "/"}:
        new_path = "/v1/audio/transcriptions"
    else:
        new_path = "/v1/audio/transcriptions"
    return urlunsplit((scheme, netloc, new_path, "", ""))


def pcm16_to_wav(
    pcm_bytes: bytes,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    channels: int = DEFAULT_CHANNELS,
    sample_width: int = 2,
) -> bytes:
    """Encode raw little-endian PCM bytes into an in-memory RIFF/WAVE payload."""
    if not isinstance(pcm_bytes, bytes) or not pcm_bytes:
        raise VoiceError("empty_audio")
    if len(pcm_bytes) > MAX_INPUT_AUDIO_BYTES:
        raise VoiceError("audio_limit_exceeded")
    if type(sample_rate) is not int or not 8_000 <= sample_rate <= 192_000:
        raise VoiceError("invalid_audio_format")
    if type(channels) is not int or channels not in (1, 2):
        raise VoiceError("invalid_audio_format")
    if type(sample_width) is not int or sample_width not in (1, 2, 3, 4):
        raise VoiceError("invalid_audio_format")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(sample_width)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm_bytes)
    return buf.getvalue()


def _is_wav_payload(payload: bytes) -> bool:
    return len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WAVE"


def _chunk_audio_bytes(data: bytes) -> list[bytes]:
    if not data:
        return []
    return [
        data[offset : offset + MAX_INPUT_FRAME_BYTES]
        for offset in range(0, len(data), MAX_INPUT_FRAME_BYTES)
    ]


class _QtAudioFormatProtocol(Protocol):
    def setSampleRate(self, sampleRate: int) -> None: ...

    def setChannelCount(self, channelCount: int) -> None: ...

    def setSampleFormat(self, format: object) -> None: ...

    def sampleRate(self) -> int: ...

    def channelCount(self) -> int: ...

    def sampleFormat(self) -> object: ...

    def isValid(self) -> bool: ...


class _QtAudioDeviceProtocol(Protocol):
    def isNull(self) -> bool: ...

    def isFormatSupported(self, format: _QtAudioFormatProtocol) -> bool: ...

    def preferredFormat(self) -> _QtAudioFormatProtocol: ...


class _QtByteArrayProtocol(Protocol):
    def data(self) -> bytes | bytearray | memoryview: ...


class _QtBufferProtocol(Protocol):
    def open(self, mode: object) -> bool: ...

    def close(self) -> None: ...

    def data(self) -> _QtByteArrayProtocol: ...

    def setData(self, data: object) -> None: ...


class _QtAudioSourceProtocol(Protocol):
    def setBufferSize(self, bytes: int) -> None: ...

    def start(self, device: _QtBufferProtocol) -> None: ...

    def stop(self) -> None: ...

    def error(self) -> object: ...


class MicrophoneRecorder(Protocol):
    """Protocol for bounded, opt-in push-to-talk audio capture."""

    @property
    def available(self) -> bool: ...

    @property
    def headless(self) -> bool: ...

    @property
    def reason(self) -> str | None: ...

    @property
    def sample_rate(self) -> int: ...

    @property
    def channels(self) -> int: ...

    def start(self) -> bool: ...

    def stop_and_drain(self) -> list[bytes]: ...

    def discard(self) -> None: ...


class QtMicrophoneRecorder:
    """Opt-in push-to-talk PCM recorder using ``PySide6.QtMultimedia.QAudioSource``."""

    def __init__(
        self,
        *,
        headless: bool | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
        audio_inputs_fn: Callable[[], Sequence[_QtAudioDeviceProtocol]] | None = None,
        default_input_fn: Callable[[], _QtAudioDeviceProtocol] | None = None,
        source_factory: Callable[
            [_QtAudioDeviceProtocol, _QtAudioFormatProtocol], _QtAudioSourceProtocol
        ]
        | None = None,
        buffer_factory: Callable[[], _QtBufferProtocol] | None = None,
    ) -> None:
        if type(sample_rate) is not int or not 8_000 <= sample_rate <= 192_000:
            raise VoiceError("invalid_audio_format")
        if type(channels) is not int or channels not in (1, 2):
            raise VoiceError("invalid_audio_format")

        self._headless = _is_headless_env() if headless is None else bool(headless)
        self._sample_rate = sample_rate
        self._channels = channels
        self._audio_inputs_fn = audio_inputs_fn
        self._default_input_fn = default_input_fn
        self._source_factory = source_factory
        self._buffer_factory = buffer_factory

        self._lock = threading.RLock()
        self._recording = False
        self._active_source: _QtAudioSourceProtocol | None = None
        self._active_buffer: _QtBufferProtocol | None = None
        self._active_sample_rate = sample_rate
        self._active_channels = channels
        self._available = False
        self._reason: str | None = "headless_offscreen" if self._headless else None

        self._qt_core_mod: object | None = None
        self._qt_mm_mod: object | None = None

        if not self._headless:
            self._probe_devices()

    def _load_qt_modules(self) -> bool:
        if self._qt_core_mod is not None and self._qt_mm_mod is not None:
            return True
        try:
            self._qt_core_mod = importlib.import_module("PySide6.QtCore")
            self._qt_mm_mod = importlib.import_module("PySide6.QtMultimedia")
            return True
        except ModuleNotFoundError:
            self._available = False
            self._reason = "pyside6_unavailable"
            return False
        except Exception:
            self._available = False
            self._reason = "audio_init_failed"
            return False

    def _probe_devices(self) -> _QtAudioDeviceProtocol | None:
        if self._headless:
            self._available = False
            self._reason = "headless_offscreen"
            return None

        inputs_fn = self._audio_inputs_fn
        default_fn = self._default_input_fn

        if inputs_fn is None or default_fn is None:
            if not self._load_qt_modules():
                return None
            media_devices_cls = getattr(self._qt_mm_mod, "QMediaDevices", None)
            if media_devices_cls is None:
                self._available = False
                self._reason = "audio_init_failed"
                return None
            if inputs_fn is None:
                raw_inputs = getattr(media_devices_cls, "audioInputs", None)
                if callable(raw_inputs):
                    inputs_fn = cast(
                        "Callable[[], Sequence[_QtAudioDeviceProtocol]]",
                        raw_inputs,
                    )
            if default_fn is None:
                raw_default = getattr(media_devices_cls, "defaultAudioInput", None)
                if callable(raw_default):
                    default_fn = cast(
                        "Callable[[], _QtAudioDeviceProtocol]",
                        raw_default,
                    )

        if inputs_fn is None or default_fn is None:
            self._available = False
            self._reason = "audio_init_failed"
            return None

        try:
            inputs = [dev for dev in inputs_fn() if not dev.isNull()]
            default_dev = default_fn()
        except Exception:
            self._available = False
            self._reason = "no_audio_input"
            return None

        if not inputs or default_dev.isNull():
            self._available = False
            self._reason = "no_audio_input"
            return None

        self._available = True
        self._reason = None
        return default_dev

    @property
    def available(self) -> bool:
        return self._available

    @property
    def headless(self) -> bool:
        return self._headless

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def sample_rate(self) -> int:
        return self._active_sample_rate

    @property
    def channels(self) -> int:
        return self._active_channels

    @property
    def recording(self) -> bool:
        with self._lock:
            return self._recording

    def start(self) -> bool:
        with self._lock:
            if self._recording or self._headless:
                return False
            device = self._probe_devices()
            if device is None:
                return False

            if self._source_factory is None or self._buffer_factory is None:
                if not self._load_qt_modules():
                    return False

            fmt = self._build_audio_format(device)
            if fmt is None:
                return False

            source: _QtAudioSourceProtocol | None = None
            buf: _QtBufferProtocol | None = None
            try:
                source = self._create_source(device, fmt)
                buf = self._create_buffer()
            except Exception:
                self._cleanup_active_locked()
                return False

            if source is None or buf is None:
                if source is not None:
                    try:
                        source.stop()
                    except Exception:
                        pass
                self._wipe_buffer(buf)
                self._cleanup_active_locked()
                return False

            try:
                source.setBufferSize(MAX_INPUT_FRAME_BYTES)
            except Exception:
                pass

            open_mode = self._read_write_flag()
            try:
                if not buf.open(open_mode):
                    try:
                        source.stop()
                    except Exception:
                        pass
                    self._wipe_buffer(buf)
                    self._cleanup_active_locked()
                    return False
                source.start(buf)
                no_error = self._qt_no_error()
                err = source.error()
                if no_error is not None and err not in (0, no_error):
                    source.stop()
                    self._wipe_buffer(buf)
                    return False
            except Exception:
                try:
                    source.stop()
                except Exception:
                    pass
                self._wipe_buffer(buf)
                return False

            self._active_source = source
            self._active_buffer = buf
            self._recording = True
            return True

    def _build_audio_format(self, device: _QtAudioDeviceProtocol) -> _QtAudioFormatProtocol | None:
        if self._qt_mm_mod is None:
            if not self._load_qt_modules():
                return None
        fmt_cls = getattr(self._qt_mm_mod, "QAudioFormat", None)
        if not callable(fmt_cls):
            return None
        sample_fmt_enum = getattr(fmt_cls, "SampleFormat", None)
        int16_fmt = getattr(sample_fmt_enum, "Int16", None)
        if int16_fmt is None:
            return None

        fmt = cast("_QtAudioFormatProtocol", fmt_cls())
        fmt.setSampleRate(self._sample_rate)
        fmt.setChannelCount(self._channels)
        fmt.setSampleFormat(int16_fmt)
        if device.isFormatSupported(fmt):
            self._active_sample_rate = self._sample_rate
            self._active_channels = self._channels
            return fmt

        preferred = device.preferredFormat()
        if (
            preferred.isValid()
            and preferred.sampleFormat() == int16_fmt
            and 8_000 <= preferred.sampleRate() <= 192_000
            and preferred.channelCount() in (1, 2)
        ):
            self._active_sample_rate = preferred.sampleRate()
            self._active_channels = preferred.channelCount()
            return preferred
        return None

    def _create_source(
        self, device: _QtAudioDeviceProtocol, fmt: _QtAudioFormatProtocol
    ) -> _QtAudioSourceProtocol | None:
        if self._source_factory is not None:
            return self._source_factory(device, fmt)
        source_cls = getattr(self._qt_mm_mod, "QAudioSource", None)
        if not callable(source_cls):
            return None
        return cast("_QtAudioSourceProtocol", source_cls(device, fmt))

    def _create_buffer(self) -> _QtBufferProtocol | None:
        if self._buffer_factory is not None:
            return self._buffer_factory()
        buffer_cls = getattr(self._qt_core_mod, "QBuffer", None)
        if not callable(buffer_cls):
            return None
        return cast("_QtBufferProtocol", buffer_cls())

    def _read_write_flag(self) -> object:
        iodevice_base = getattr(self._qt_core_mod, "QIODeviceBase", None)
        open_flag = getattr(iodevice_base, "OpenModeFlag", None)
        return getattr(open_flag, "ReadWrite", 3)

    def _qt_no_error(self) -> object:
        if self._qt_mm_mod is None:
            return None
        qt_audio = getattr(self._qt_mm_mod, "QtAudio", None)
        err_enum = getattr(qt_audio, "Error", None)
        return getattr(err_enum, "NoError", None)

    def _wipe_buffer(self, buf: _QtBufferProtocol | None) -> None:
        if buf is None:
            return
        try:
            buf.close()
        except Exception:
            pass
        try:
            if self._qt_core_mod is not None:
                qba_cls = getattr(self._qt_core_mod, "QByteArray", None)
                if callable(qba_cls):
                    buf.setData(qba_cls())
                    return
            buf.setData(b"")
        except Exception:
            pass

    def _cleanup_active_locked(self) -> None:
        source = self._active_source
        buf = self._active_buffer
        self._active_source = None
        self._active_buffer = None
        self._recording = False
        if source is not None:
            try:
                source.stop()
            except Exception:
                pass
        self._wipe_buffer(buf)

    def stop_and_drain(self) -> list[bytes]:
        with self._lock:
            if not self._recording:
                self._cleanup_active_locked()
                return []
            source = self._active_source
            buf = self._active_buffer
            self._active_source = None
            self._active_buffer = None
            self._recording = False

            raw_data = bytearray()
            try:
                if source is not None:
                    try:
                        source.stop()
                    except Exception:
                        pass
                if buf is not None:
                    raw_view = buf.data().data()
                    if len(raw_view) > MAX_INPUT_AUDIO_BYTES:
                        del raw_view
                        raise VoiceError("audio_limit_exceeded")
                    raw_data.extend(raw_view)
                    del raw_view
            finally:
                self._wipe_buffer(buf)

            try:
                if not raw_data:
                    return []
                if len(raw_data) > MAX_INPUT_AUDIO_BYTES:
                    raise VoiceError("audio_limit_exceeded")
                return _chunk_audio_bytes(bytes(raw_data))
            finally:
                raw_data[:] = b"\x00" * len(raw_data)
                raw_data.clear()

    def discard(self) -> None:
        with self._lock:
            self._cleanup_active_locked()


class _HttpResponseProtocol(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def is_redirect(self) -> bool: ...

    def aiter_bytes(self, chunk_size: int | None = None) -> AsyncIterator[bytes]: ...


class _HttpClientProtocol(Protocol):
    def stream(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None = ...,
        files: Mapping[str, tuple[str, bytes, str]] | None = ...,
        headers: Mapping[str, str] | None = ...,
        follow_redirects: bool = ...,
        timeout: float = ...,
    ) -> AbstractAsyncContextManager[_HttpResponseProtocol]: ...


class DesktopSpeechToText:
    """Concrete ``SpeechToText`` adapter backed by OpenAI audio transcriptions or ``transcribe_fn``."""

    def __init__(
        self,
        *,
        provider_config: ProviderConfig | None = None,
        desktop_config: DesktopConfig | None = None,
        api_key: str | None = None,
        endpoint: str | None = None,
        model: str = DEFAULT_STT_MODEL,
        timeout_seconds: float = 20.0,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
        http_client: _HttpClientProtocol | None = None,
        transport: object | None = None,
        transcribe_fn: Callable[[bytes], Awaitable[str] | str] | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip() or len(model.strip()) > 120:
            raise VoiceError("invalid_stt_model")
        if _has_control_chars(model):
            raise VoiceError("invalid_stt_model")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int | float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or timeout_seconds > 120
        ):
            raise VoiceError("invalid_stt_timeout")
        if type(sample_rate) is not int or not 8_000 <= sample_rate <= 192_000:
            raise VoiceError("invalid_audio_format")
        if type(channels) is not int or channels not in (1, 2):
            raise VoiceError("invalid_audio_format")

        resolved_provider = provider_config
        if resolved_provider is None and desktop_config is not None:
            resolved_provider = desktop_config.openai

        unsupported_provider = False
        if resolved_provider is not None and resolved_provider.provider != "openai":
            unsupported_provider = True
            resolved_provider = None

        raw_key = (
            api_key
            if api_key is not None
            else (resolved_provider.api_key if resolved_provider is not None else None)
        )
        cleaned_key: str | None = None
        if raw_key is not None:
            if not isinstance(raw_key, str) or _has_control_chars(raw_key):
                raise VoiceError("invalid_api_key")
            stripped_key = raw_key.strip()
            if stripped_key:
                cleaned_key = stripped_key

        if endpoint is not None:
            scheme, netloc, path = _validate_stt_url(endpoint)
            if path.endswith("/chat/completions") or path.rstrip("/") == "/v1" or path in {"", "/"}:
                resolved_endpoint = derive_transcription_endpoint(endpoint)
            else:
                resolved_endpoint = urlunsplit((scheme, netloc, path, "", ""))
        elif resolved_provider is not None:
            resolved_endpoint = derive_transcription_endpoint(resolved_provider.endpoint)
        else:
            resolved_endpoint = DEFAULT_STT_ENDPOINT

        self._api_key = cleaned_key
        self._endpoint = resolved_endpoint
        self._model = model.strip()
        self._timeout_seconds = float(timeout_seconds)
        self._sample_rate = sample_rate
        self._channels = channels
        self._http_client = http_client
        self._transport = transport
        self._transcribe_fn = transcribe_fn
        self._reason: str | None = None

        if self._transcribe_fn is None and self._api_key is None:
            self._reason = (
                "unsupported_stt_provider" if unsupported_provider else "stt_not_configured"
            )

    def __repr__(self) -> str:
        return f"DesktopSpeechToText(available={self.available!r}, model={self._model!r})"

    @property
    def available(self) -> bool:
        return self._transcribe_fn is not None or self._api_key is not None

    @property
    def reason(self) -> str | None:
        return self._reason

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def model(self) -> str:
        return self._model

    async def recognize(self, audio: AsyncIterator[bytes]) -> str:
        raw_buffer = bytearray()
        try:
            async for frame in bounded_microphone(audio):
                if len(raw_buffer) + len(frame) > MAX_INPUT_AUDIO_BYTES:
                    raise VoiceError("audio_limit_exceeded")
                raw_buffer.extend(frame)
            if not raw_buffer:
                raise VoiceError("empty_audio")
            audio_payload = bytes(raw_buffer)
        finally:
            raw_buffer[:] = b"\x00" * len(raw_buffer)
            raw_buffer.clear()

        if self._transcribe_fn is not None:
            maybe_text = self._transcribe_fn(audio_payload)
            if inspect.isawaitable(maybe_text):
                raw_text = await maybe_text
            else:
                raw_text = maybe_text
        else:
            if self._api_key is None:
                raise VoiceError("stt_not_configured")
            wav_payload = (
                audio_payload
                if _is_wav_payload(audio_payload)
                else pcm16_to_wav(
                    audio_payload,
                    sample_rate=self._sample_rate,
                    channels=self._channels,
                )
            )
            raw_text = await self._transcribe_openai(wav_payload)

        if not isinstance(raw_text, str):
            raise VoiceError("empty_transcript")
        cleaned = raw_text.strip()
        if not cleaned:
            raise VoiceError("empty_transcript")
        if len(cleaned) > MAX_TRANSCRIPT_CHARS:
            raise VoiceError("transcript_too_large")
        return cleaned

    async def _transcribe_openai(self, wav_bytes: bytes) -> str:
        if self._api_key is None:
            raise VoiceError("stt_not_configured")

        httpx_mod = importlib.import_module("httpx")
        timeout_exc = cast("type[BaseException]", httpx_mod.TimeoutException)
        http_err = cast("type[BaseException]", httpx_mod.HTTPError)

        if self._http_client is not None:
            return await self._post_transcription(
                self._http_client,
                wav_bytes,
                timeout_exc=timeout_exc,
                http_err=http_err,
            )

        async_client_cls = httpx_mod.AsyncClient
        client_kwargs: dict[str, object] = {
            "timeout": self._timeout_seconds,
            "follow_redirects": False,
        }
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        async with async_client_cls(**client_kwargs) as client:
            return await self._post_transcription(
                cast("_HttpClientProtocol", client),
                wav_bytes,
                timeout_exc=timeout_exc,
                http_err=http_err,
            )

    async def _post_transcription(
        self,
        client: _HttpClientProtocol,
        wav_bytes: bytes,
        *,
        timeout_exc: type[BaseException],
        http_err: type[BaseException],
    ) -> str:
        assert self._api_key is not None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        files = {
            "file": ("speech.wav", wav_bytes, "audio/wav"),
        }
        data = {
            "model": self._model,
        }
        body_buf = bytearray()
        try:
            try:
                async with client.stream(
                    "POST",
                    self._endpoint,
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=self._timeout_seconds,
                    follow_redirects=False,
                ) as response:
                    status = response.status_code
                    if response.is_redirect or 300 <= status < 400:
                        raise VoiceError("stt_redirect_denied") from None
                    if status in (401, 403):
                        raise VoiceError("stt_auth_failed") from None
                    if status == 429:
                        raise VoiceError("stt_rate_limited") from None
                    if status in (400, 422):
                        raise VoiceError("stt_invalid_request") from None
                    if status >= 500:
                        raise VoiceError("stt_provider_unavailable") from None
                    if not 200 <= status < 300:
                        raise VoiceError("stt_request_failed") from None

                    async for chunk in response.aiter_bytes():
                        if len(body_buf) + len(chunk) > MAX_STT_RESPONSE_BYTES:
                            raise VoiceError("stt_response_too_large") from None
                        body_buf.extend(chunk)
            except VoiceError:
                raise
            except Exception as exc:
                if isinstance(exc, timeout_exc):
                    raise VoiceError("stt_timeout") from None
                if isinstance(exc, http_err) or isinstance(exc, OSError):
                    raise VoiceError("stt_network_error") from None
                raise VoiceError("stt_request_failed") from None

            try:
                decoded = bytes(body_buf).decode("utf-8")
                parsed = json.loads(decoded)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise VoiceError("stt_invalid_response") from None
        finally:
            body_buf[:] = b"\x00" * len(body_buf)
            body_buf.clear()

        if not isinstance(parsed, dict) or not isinstance(parsed.get("text"), str):
            raise VoiceError("stt_invalid_response") from None
        return str(parsed["text"])
