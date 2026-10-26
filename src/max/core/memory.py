"""Curated long-term memory (bounded, always small enough for the prompt).

Modeled on Hermes Agent's MEMORY.md idea: a small, explicitly edited note
store that is snapshotted into the system prompt, with a hard character cap
so it can never silently bloat the context window.
"""

from __future__ import annotations

import time
from pathlib import Path

HEADER = "# MAX memory\n"
DEFAULT_MAX_CHARS = 4000


class MemoryStore:
    def __init__(self, path: Path, max_chars: int = DEFAULT_MAX_CHARS):
        self.path = Path(path)
        self.max_chars = max_chars
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(HEADER, encoding="utf-8")

    def add(self, note: str) -> str:
        note = " ".join(str(note).split())
        if not note:
            raise ValueError("empty note")
        if len(note) > 500:
            raise ValueError("note too long (max 500 chars)")
        stamp = time.strftime("%Y-%m-%d")
        candidate = self.read() + f"- [{stamp}] {note}\n"
        if len(candidate) > self.max_chars:
            raise ValueError(
                f"memory limit reached ({self.max_chars} chars); remove an old note first"
            )
        self.path.write_text(candidate, encoding="utf-8")
        return f"noted ({len(candidate)}/{self.max_chars} chars)"

    def read(self) -> str:
        if not self.path.is_file():
            return HEADER
        return self.path.read_text(encoding="utf-8")

    def entries(self) -> list[str]:
        return [
            line for line in self.read().splitlines()
            if line.startswith("- [")
        ]

    def snapshot(self, limit: int = 1500) -> str:
        """Prompt-sized view: header + most recent notes, capped."""
        text = self.read().strip()
        if len(text) > limit:
            lines = text.splitlines()
            trimmed = [lines[0]]
            for line in reversed(lines[1:]):
                if sum(len(x) + 1 for x in trimmed) + len(line) > limit:
                    break
                trimmed.insert(1, line)
            text = "\n".join(trimmed) + "\n(older notes omitted)"
        return text
