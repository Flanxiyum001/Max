"""Opt-in, local-only conversation archive inspired by Hermes session search.

This module never registers a model tool. Search results are shown to the owner
only; sending them to a provider requires a separate, explicit chat message.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path


class SessionArchive:
    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir / "sessions.sqlite3"
        data_dir.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("session_archive_symlink")
        self.session_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS messages ("
                "id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, "
                "role TEXT NOT NULL, content TEXT NOT NULL, "
                "created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )

    def _connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            self.path.touch(mode=0o600)
        if self.path.is_symlink():
            raise ValueError("session_archive_symlink")
        return sqlite3.connect(self.path, timeout=5)

    def append(self, user: str, assistant: str) -> None:
        with self._connect() as db:
            db.executemany(
                "INSERT INTO messages(session_id, role, content) VALUES (?, ?, ?)",
                (
                    (self.session_id, "user", user[:20_000]),
                    (self.session_id, "assistant", assistant[:20_000]),
                ),
            )

    def new_session(self) -> None:
        self.session_id = uuid.uuid4().hex

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, str]]:
        query = query.strip()
        if not 2 <= len(query) <= 200 or not 1 <= limit <= 20:
            raise ValueError("invalid_archive_search")
        # Literal substring search avoids FTS query operators and wildcard input.
        with self._connect() as db:
            rows = db.execute(
                "SELECT session_id, role, substr(content, 1, 400), created "
                "FROM messages WHERE instr(lower(content), lower(?)) > 0 "
                "ORDER BY id DESC LIMIT ?",
                (query, limit),
            ).fetchall()
        return [
            dict(session_id=row[0], role=row[1], excerpt=row[2], created=row[3]) for row in rows
        ]
