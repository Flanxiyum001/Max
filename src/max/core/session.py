"""Session persistence: append-only JSONL transcripts under data/sessions."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ROLES = {"user", "assistant", "system", "tool"}
MAX_HISTORY_MESSAGES = 60


class SessionStore:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_id(session_id: str) -> str:
        if not _SESSION_ID.match(session_id or ""):
            raise ValueError("session id must match [A-Za-z0-9_-]{1,64}")
        return session_id

    def path(self, session_id: str) -> Path:
        return self.dir / f"{self.validate_id(session_id)}.jsonl"

    def append(self, session_id: str, role: str, content: str, **meta) -> dict:
        if role not in _ROLES:
            raise ValueError(f"invalid role {role!r}")
        entry = {
            "ts": round(time.time(), 3),
            "role": role,
            "content": str(content),
            **meta,
        }
        with self.path(session_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def load(self, session_id: str) -> list[dict]:
        path = self.path(session_id)
        if not path.is_file():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # tolerate a torn trailing line
        return out

    def history(self, session_id: str, limit: int = MAX_HISTORY_MESSAGES) -> list[dict]:
        """Conversation-shaped history for the provider (no meta fields)."""
        messages = [
            {"role": entry["role"], "content": entry["content"]}
            for entry in self.load(session_id)
            if entry.get("role") in {"user", "assistant"} and entry.get("content")
        ]
        return messages[-limit:]

    def list_ids(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.jsonl"))
