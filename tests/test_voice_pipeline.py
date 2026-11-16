from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest

from max_core.voice import VoiceError, VoiceTurnPipeline, bounded_microphone, sentence_chunks


async def audio() -> AsyncIterator[bytes]:
    yield b"microphone"


class STT:
    async def recognize(self, source: AsyncIterator[bytes]) -> str:
        assert b"".join([frame async for frame in source]) == b"microphone"
        return "Hello MAX"


class TTS:
    def __init__(self) -> None:
        self.sentences: list[str] = []

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        self.sentences.append(text)
        yield text.encode()


class Sink:
    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.stops = 0

    async def play(self, frame: bytes) -> None:
        self.frames.append(frame)

    async def stop(self) -> None:
        self.stops += 1


class Gateway:
    def __init__(self, parts: list[tuple[str, str]]) -> None:
        self.parts = parts
        self.requests: list[str] = []

    async def stream(self, request: str) -> AsyncIterator[Any]:
        self.requests.append(request)
        for kind, text in self.parts:
            yield SimpleNamespace(kind=kind, text=text)


def test_voice_pipeline_committed_stt_and_streamed_speech() -> None:
    async def check() -> None:
        gateway = Gateway([("start", ""), ("content_delta", "Good morning. Next"),
                           ("content_delta", " sentence!"), ("tool_proposal", "ignored"),
                           ("complete", "")])
        tts, sink = TTS(), Sink()
        result = await VoiceTurnPipeline(gateway).run(
            audio(), STT(), lambda text: f"approved:{text}", tts, sink, asyncio.Event()
        )
        assert gateway.requests == ["approved:Hello MAX"]
        assert tts.sentences == ["Good morning.", "Next sentence!"]
        assert sink.frames == [b"Good morning.", b"Next sentence!"]
        assert result.audio_frames == 2 and result.speech_chunks == 2
        assert result.first_audio_ms_after_transcript is not None
        assert sink.stops == 0
    asyncio.run(check())


def test_unfinished_gateway_stream_fails_closed_and_stops_audio() -> None:
    async def check() -> None:
        sink = Sink()
        with pytest.raises(VoiceError, match="gateway_stream_incomplete"):
            await VoiceTurnPipeline(Gateway([("content_delta", "partial")])).run(
                audio(), STT(), lambda text: text, TTS(), sink, asyncio.Event()
            )
        assert sink.stops == 1
    asyncio.run(check())


def test_cancellation_during_stalled_gateway_closes_turn() -> None:
    class StalledGateway:
        async def stream(self, request: str) -> AsyncIterator[Any]:
            yield SimpleNamespace(kind="start", text="")
            await asyncio.Event().wait()
            yield SimpleNamespace(kind="complete", text="")

    async def check() -> None:
        stop, sink = asyncio.Event(), Sink()
        turn = asyncio.create_task(VoiceTurnPipeline(StalledGateway()).run(
            audio(), STT(), lambda text: text, TTS(), sink, stop
        ))
        await asyncio.sleep(0)
        stop.set()
        result = await asyncio.wait_for(turn, timeout=0.5)
        assert result.interrupted
        assert sink.stops == 1
        assert sink.frames == []
    asyncio.run(check())


def test_limited_audio_rejects_oversized_frame() -> None:
    async def oversized() -> AsyncIterator[bytes]:
        yield b"a" * (256 * 1024 + 1)

    async def check() -> None:
        with pytest.raises(VoiceError, match="invalid_audio_frame"):
            _ = [part async for part in bounded_microphone(oversized())]
    asyncio.run(check())


def test_sentence_chunking_caps_unpunctuated_output() -> None:
    async def chunks() -> AsyncIterator[Any]:
        yield SimpleNamespace(kind="content_delta", text="A " * 105)
        yield SimpleNamespace(kind="complete", text="")

    async def check() -> None:
        parts = [part async for part in sentence_chunks(chunks())]
        assert len(parts) >= 2
        assert " ".join(parts).replace("  ", " ") == ("A " * 105).strip()
    asyncio.run(check())


def test_already_interrupted_does_not_start_microphone_or_model() -> None:
    async def unavailable() -> AsyncIterator[bytes]:
        raise AssertionError("audio source was read")
        yield b"not used"

    async def check() -> None:
        event, sink = asyncio.Event(), Sink()
        event.set()
        result = await VoiceTurnPipeline(Gateway([])).run(
            unavailable(), STT(), lambda text: text, TTS(), sink, event
        )
        assert result.interrupted and sink.stops == 1
    asyncio.run(check())
