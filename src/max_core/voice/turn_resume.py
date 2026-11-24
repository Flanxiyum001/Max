"""Bounded sentence-level speech resumption without implicit microphone activation."""

from __future__ import annotations

import re


def is_resume_request(text: str) -> bool:
    normalized = " ".join(re.findall(r"[a-z]+", text.casefold()))
    return normalized in {
        "continue",
        "continue please",
        "please continue",
        "go on",
        "carry on",
        "resume",
        "resume please",
        "keep going",
    }


class SpeechCursor:
    """Replay the interrupted sentence, never a completed sentence."""

    def __init__(self) -> None:
        self._chunks: list[str] = []
        self._index = 0

    def begin(self, text: str) -> None:
        self._chunks = [
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+", text[:1500].strip())
            if sentence.strip()
        ]
        self._index = 0

    @property
    def current(self) -> str:
        if self._index >= len(self._chunks):
            return ""
        return self._chunks[self._index]

    def advance(self) -> None:
        if self._index < len(self._chunks):
            self._index += 1

    def clear(self) -> None:
        self._chunks.clear()
        self._index = 0
