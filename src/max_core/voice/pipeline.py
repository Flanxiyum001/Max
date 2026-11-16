"""Cancellable voice-turn pipeline using only previously authorised gateway streams.

Audio capture, AEC, VAD, STT/TTS and playback are injected, never auto-started.
No audio or transcripts are persisted by this module.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from max_core.gateway.contracts import GatewayRequest, StreamEvent

MAX_INPUT_AUDIO_BYTES = 8 * 1024 * 1024
MAX_INPUT_FRAME_BYTES = 256 * 1024
MAX_OUTPUT_FRAME_BYTES = 256 * 1024
MAX_TRANSCRIPT_CHARS = 4096
MAX_SPEECH_CHUNK_CHARS = 180


class VoiceError(Exception):
    """Safe operational failure without raw audio or transcript in the error."""


class SpeechToText(Protocol):
    async def recognize(self, audio: AsyncIterator[bytes]) -> str: ...


class TextToSpeech(Protocol):
    def stream(self, text: str) -> AsyncIterator[bytes]: ...


class AudioSink(Protocol):
    async def play(self, frame: bytes) -> None: ...

    async def stop(self) -> None: ...


class TextGateway(Protocol):
    def stream(self, request: GatewayRequest) -> AsyncIterator[StreamEvent]: ...


@dataclass(frozen=True, slots=True)
class VoiceTurnResult:
    interrupted: bool
    speech_chunks: int
    audio_frames: int
    # Measured after STT commits the transcript, not from the microphone endpoint.
    first_audio_ms_after_transcript: float | None


async def bounded_microphone(audio: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Enforce an upper bound even when the microphone source is untrusted."""
    total = 0
    async for frame in audio:
        if not isinstance(frame, bytes) or not frame or len(frame) > MAX_INPUT_FRAME_BYTES:
            raise VoiceError("invalid_audio_frame")
        total += len(frame)
        if total > MAX_INPUT_AUDIO_BYTES:
            raise VoiceError("audio_limit_exceeded")
        yield frame


def _boundary(buffer: str) -> int:
    """Return the first speech-ready boundary without waiting for full completion."""
    for index, char in enumerate(buffer):
        if char in ".!?\n" and (index + 1 == len(buffer) or buffer[index + 1].isspace()):
            return index + 1
    if len(buffer) >= MAX_SPEECH_CHUNK_CHARS:
        cut = buffer.rfind(" ", 1, MAX_SPEECH_CHUNK_CHARS + 1)
        return cut + 1 if cut >= MAX_SPEECH_CHUNK_CHARS // 2 else MAX_SPEECH_CHUNK_CHARS
    return 0


async def sentence_chunks(events: AsyncIterator[StreamEvent]) -> AsyncIterator[str]:
    """Consume only text events; inert tool proposals are never voiced or executed."""
    buffer = ""
    completed = False
    async for event in events:
        if event.kind == "content_delta":
            buffer += event.text
            while length := _boundary(buffer):
                ready, buffer = buffer[:length].strip(), buffer[length:]
                if ready:
                    yield ready
        elif event.kind == "complete":
            completed = True
            break
    if not completed:
        raise VoiceError("gateway_stream_incomplete")
    if buffer.strip():
        yield buffer.strip()


class VoiceTurnPipeline:
    """Speech -> committed text -> trusted Gateway -> streaming TTS -> sink.

    The caller owns the cancellation event and must set it on verified barge-in.
    Hardware output implementations must clear queued audio inside ``stop``.
    """

    def __init__(self, gateway: TextGateway) -> None:
        self.gateway = gateway

    async def run(
        self,
        audio: AsyncIterator[bytes],
        stt: SpeechToText,
        build_request: Callable[[str], GatewayRequest],
        tts: TextToSpeech,
        sink: AudioSink,
        interrupt: asyncio.Event,
    ) -> VoiceTurnResult:
        async def work() -> VoiceTurnResult:
            transcript = await stt.recognize(bounded_microphone(audio))
            if not isinstance(transcript, str) or not transcript.strip():
                raise VoiceError("empty_transcript")
            if len(transcript) > MAX_TRANSCRIPT_CHARS:
                raise VoiceError("transcript_too_large")
            # This must be a trusted factory; Gateway applies all model/privacy gates.
            request = build_request(transcript)
            queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=2)
            chunks = 0
            frames = 0
            first_ms: float | None = None
            started = time.monotonic()

            async def produce() -> None:
                async for sentence in sentence_chunks(self.gateway.stream(request)):
                    await queue.put(sentence)
                await queue.put(None)

            async def consume() -> None:
                nonlocal chunks, frames, first_ms
                while (sentence := await queue.get()) is not None:
                    chunks += 1
                    async for frame in tts.stream(sentence):
                        if (
                            not isinstance(frame, bytes)
                            or not frame
                            or len(frame) > MAX_OUTPUT_FRAME_BYTES
                        ):
                            raise VoiceError("invalid_speech_frame")
                        await sink.play(frame)
                        frames += 1
                        if first_ms is None:
                            first_ms = (time.monotonic() - started) * 1000

            producer = asyncio.create_task(produce())
            consumer = asyncio.create_task(consume())
            try:
                await asyncio.gather(producer, consumer)
            finally:
                for task in (producer, consumer):
                    task.cancel()
                await asyncio.gather(producer, consumer, return_exceptions=True)
            return VoiceTurnResult(False, chunks, frames, first_ms)

        if interrupt.is_set():
            await sink.stop()
            return VoiceTurnResult(True, 0, 0, None)
        active = asyncio.create_task(work())
        interrupt_waiter = asyncio.create_task(interrupt.wait())
        try:
            done, _ = await asyncio.wait(
                {active, interrupt_waiter}, return_when=asyncio.FIRST_COMPLETED
            )
            if interrupt_waiter in done and interrupt.is_set():
                active.cancel()
                await asyncio.gather(active, return_exceptions=True)
                await sink.stop()
                return VoiceTurnResult(True, 0, 0, None)
            return await active
        except BaseException:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            await sink.stop()
            raise
        finally:
            interrupt_waiter.cancel()
            await asyncio.gather(interrupt_waiter, return_exceptions=True)
