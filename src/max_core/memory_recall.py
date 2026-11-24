"""Persistent semantic memory: hybrid recall and the owner-approved MEMORY.md journal.

Derived, rebuildable SQLite index (never authoritative) over three bounded
sources: owner notes (``*.txt`` in ``MAX_NOTES_DIR``), terminal Task Journal
records, and the owner's ``MEMORY.md`` facts. Embeddings are deterministic
local feature hashing - no model download, no cloud egress. Vector recall uses
the owner-installed ``sqlite-vec`` extension when present and degrades to FTS5
keyword recall otherwise; both channels are recall only. ``MEMORY.md`` content
changes only inside an R1 owner-approved tool handler; nothing here proposes
facts automatically or grants any permission.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sqlite3
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from max_core.store import TaskStore

_DIM = 256
_MAX_QUERY_CHARS = 300
_MAX_SNIPPET = 240
_MAX_NOTES = 500
_MAX_NOTE_CHARS = 4_000
_MAX_TASKS = 200
_MAX_TASK_CHARS = 2_000
_MAX_MEM_ENTRIES = 512
_MAX_ENTRY_CHARS = 280
_TOKENS = re.compile(r"[a-z0-9]+")
_UNSAFE_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TERMINAL = ("completed", "cancelled", "failed")
_RRF_K = 60


class MemoryRecallError(ValueError):
    """Fixed operational code; never includes SQL, file paths or memory contents."""

    def __init__(self, code: str, status_code: int = 422) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _load_sqlite_vec() -> Any | None:
    try:
        import sqlite_vec  # type: ignore[import-untyped]
    except ImportError:
        return None
    return sqlite_vec


def embed(text: str) -> list[float]:
    """Deterministic 256-dim signed feature hashing over unigrams and bigrams."""
    if not isinstance(text, str):
        raise MemoryRecallError("invalid_memory_document")
    tokens = _TOKENS.findall(text.lower())[:512]
    vec = [0.0] * _DIM
    for token in tokens:
        slot, sign = _slot(token)
        vec[slot] += -0.5 if sign else 0.5
    for first, second in zip(tokens, tokens[1:], strict=False):
        slot, sign = _slot(f"{first}_{second}")
        vec[slot] += -1.0 if sign else 1.0
    norm = math.sqrt(math.fsum(value * value for value in vec))
    if norm == 0:
        return vec
    return [value / norm for value in vec]


def _slot(token: str) -> tuple[int, bool]:
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    value = int.from_bytes(digest, "big")
    return value % _DIM, bool(value >> 63)


def validate_memory_entry(entry: object) -> str:
    if not isinstance(entry, str) or _UNSAFE_CONTROLS.search(entry):
        raise MemoryRecallError("invalid_memory_entry")
    try:
        entry.encode("utf-8")
    except UnicodeEncodeError:
        raise MemoryRecallError("invalid_memory_entry") from None
    normalized = " ".join(entry.split())
    if not 1 <= len(normalized) <= _MAX_ENTRY_CHARS or normalized.startswith(("#", "```")):
        raise MemoryRecallError("invalid_memory_entry")
    return normalized


def read_memory_entries(path: Path) -> list[str]:
    """Bounded MEMORY.md facts; headings are structure, entries are content."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        raise MemoryRecallError("memory_md_unreadable", 500) from None
    entries: list[str] = []
    for line in raw.splitlines():
        clean = " ".join(line.split())
        if clean.startswith("- "):
            clean = clean[2:].strip()
        if not clean or clean.startswith("#") or clean.startswith("```"):
            continue
        if _UNSAFE_CONTROLS.search(clean):
            raise MemoryRecallError("memory_md_unreadable", 500)
        entries.append(clean[:_MAX_ENTRY_CHARS])
        if len(entries) >= _MAX_MEM_ENTRIES:
            break
    return entries


def _write_entries(path: Path, entries: list[str]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "# MAX memory\n# Owner-approved facts only; every line needs an explicit Trust approval.\n\n"
        body += "".join(f"- {entry}\n" for entry in entries)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".memory-md-", delete=False
        )
        try:
            with handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(handle.name, path)
        except BaseException:
            os.unlink(handle.name)
            raise
    except OSError:
        raise MemoryRecallError("memory_md_write_failed", 500) from None


def append_memory_entry(path: Path, entry: object) -> dict[str, object]:
    """Case-insensitive dedupe; bounded entry count; atomic replace, never append-async."""
    clean = validate_memory_entry(entry)
    entries = read_memory_entries(path)
    if any(item.lower() == clean.lower() for item in entries):
        return {"op": "already_present", "entries": len(entries)}
    if len(entries) >= _MAX_MEM_ENTRIES:
        raise MemoryRecallError("memory_md_full", 409)
    entries.append(clean)
    _write_entries(path, entries)
    return {"op": "appended", "entries": len(entries)}


def remove_memory_entry(path: Path, entry: object) -> dict[str, object]:
    clean = validate_memory_entry(entry)
    entries = read_memory_entries(path)
    kept = [item for item in entries if item.lower() != clean.lower()]
    if len(kept) == len(entries):
        raise MemoryRecallError("entry_not_found", 404)
    _write_entries(path, kept)
    return {"op": "removed", "entries": len(kept)}


@dataclass(frozen=True, slots=True, repr=False)
class MemoryHit:
    source: str
    ref: str
    title: str
    snippet: str
    score: float


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS memory_documents ("
    "id INTEGER PRIMARY KEY, source TEXT NOT NULL, ref TEXT NOT NULL, "
    "title TEXT NOT NULL, body TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS memory_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(title, body)",
)


class MemoryRecallIndex:
    """Derived hybrid recall index; rebuilding is always safe and side-effect free."""

    def __init__(self, path: Path) -> None:
        if not isinstance(path, Path) or not str(path):
            raise MemoryRecallError("invalid_memory_index_path")
        self._path = path
        self._vector_loaded = False

    def _connect(self) -> sqlite3.Connection:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self._path, timeout=5)
            connection.execute("PRAGMA journal_mode=WAL")
            return connection
        except (OSError, sqlite3.Error):
            raise MemoryRecallError("memory_unavailable", 500) from None

    def _load_extension(self, connection: sqlite3.Connection) -> Any | None:
        """Load sqlite-vec into this specific connection; extension state is per-connection."""
        module = _load_sqlite_vec()
        if module is None:
            return None
        try:
            connection.enable_load_extension(True)
            module.load(connection)
            connection.enable_load_extension(False)
        except (AttributeError, OSError, sqlite3.Error):
            return None
        return module

    def vector_ready(self) -> bool:
        connection = self._connect()
        try:
            return self._load_extension(connection) is not None
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> bool:
        """Create tables; records whether vec0 is usable on this connection."""
        for statement in _SCHEMA:
            connection.execute(statement)
        self._vector_loaded = self._load_extension(connection) is not None
        return self._vector_loaded

    def sync(
        self,
        notes_dir: Path | None,
        tasks: Sequence[tuple[str, str]],
        memory_path: Path | None,
    ) -> bool:
        """Rebuild from bounded sources when any content changed; returns True if rebuilt."""
        documents = self._collect(notes_dir, tasks, memory_path)
        fingerprint = hashlib.sha256(
            "\n".join(f"{source}|{ref}|{body}" for source, ref, _title, body in documents).encode(
                "utf-8"
            )
        ).hexdigest()
        connection = self._connect()
        try:
            with connection:
                self._ensure_schema(connection)
                stored = connection.execute(
                    "SELECT value FROM memory_meta WHERE key = 'fingerprint'"
                ).fetchone()
                if stored is not None and stored[0] == fingerprint:
                    return False
                connection.execute("DELETE FROM memory_documents")
                connection.execute("DELETE FROM memory_fts")
                try:
                    connection.execute("DROP TABLE IF EXISTS memory_vec")
                except sqlite3.Error:
                    pass
                vector_docs: list[tuple[int, str]] = []
                for source, ref, title, body in documents:
                    cursor = connection.execute(
                        "INSERT INTO memory_documents(source, ref, title, body) VALUES (?, ?, ?, ?)",
                        (source, ref, title, body),
                    )
                    rowid = int(cursor.lastrowid or 0)
                    connection.execute(
                        "INSERT INTO memory_fts(rowid, title, body) VALUES (?, ?, ?)",
                        (rowid, title, body),
                    )
                    if self._vector_loaded:
                        vector_docs.append((rowid, body))
                if self._vector_loaded:
                    connection.execute(
                        "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0("
                        "id INTEGER PRIMARY KEY, embedding float[256])"
                    )
                    for rowid, body in vector_docs:
                        self._insert_vector(connection, rowid, embed(body))
                connection.execute(
                    "INSERT INTO memory_meta(key, value) VALUES ('fingerprint', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (fingerprint,),
                )
            return True
        except sqlite3.Error:
            raise MemoryRecallError("memory_unavailable", 500) from None
        finally:
            connection.close()

    def _insert_vector(
        self, connection: sqlite3.Connection, rowid: int, vector: list[float]
    ) -> None:
        module = _load_sqlite_vec()
        assert module is not None
        blob = module.serialize_float32(vector)
        connection.execute("INSERT INTO memory_vec(id, embedding) VALUES (?, ?)", (rowid, blob))

    def _collect(
        self,
        notes_dir: Path | None,
        tasks: Sequence[tuple[str, str]],
        memory_path: Path | None,
    ) -> list[tuple[str, str, str, str]]:
        documents: list[tuple[str, str, str, str]] = []
        if isinstance(notes_dir, Path) and notes_dir.is_dir():
            for note in sorted(notes_dir.glob("*.txt"), key=lambda item: item.stem)[:_MAX_NOTES]:
                try:
                    body = note.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                title = note.stem[:60]
                documents.append(("note", note.stem[:80], title, body[:_MAX_NOTE_CHARS]))
        for task_id, objective in tuple(tasks)[:_MAX_TASKS]:
            body = objective[:_MAX_TASK_CHARS]
            documents.append(("task", str(task_id)[:8], body[:60], body))
        if isinstance(memory_path, Path):
            for index, entry in enumerate(read_memory_entries(memory_path), start=1):
                documents.append(("memory", f"line-{index}", entry[:60], entry))
        return documents

    def search(self, query: object, limit: int = 8) -> list[MemoryHit]:
        if not isinstance(query, str):
            raise MemoryRecallError("invalid_memory_query")
        normalized = " ".join(query.split())
        if not 1 <= len(normalized) <= _MAX_QUERY_CHARS:
            raise MemoryRecallError("invalid_memory_query")
        if type(limit) is not int or not 1 <= limit <= 8:
            raise MemoryRecallError("invalid_memory_query")
        tokens = [
            token for token in dict.fromkeys(_TOKENS.findall(normalized.lower())) if len(token) >= 2
        ][:16]
        connection = self._connect()
        try:
            with connection:
                self._ensure_schema(connection)
                fts_order = self._fts_search(connection, tokens, limit * 4)
                vec_order = self._vec_search(connection, normalized, limit * 4)
        except sqlite3.Error:
            raise MemoryRecallError("memory_unavailable", 500) from None
        finally:
            connection.close()
        scores: dict[int, float] = {}
        for position, doc_id in enumerate(fts_order, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + position)
        for position, doc_id in enumerate(vec_order, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + position)
        ordered = sorted(scores, key=lambda doc: -scores[doc])[:limit]
        return self._hits(ordered, set(fts_order), tokens)

    def _fts_search(
        self, connection: sqlite3.Connection, tokens: list[str], limit: int
    ) -> list[int]:
        if not tokens:
            return []
        match_expr = " OR ".join(f'"{token}"' for token in tokens)
        try:
            rows = connection.execute(
                "SELECT rowid FROM memory_fts WHERE memory_fts MATCH ? "
                "ORDER BY bm25(memory_fts) LIMIT ?",
                (match_expr, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [int(row[0]) for row in rows]

    def _vec_search(self, connection: sqlite3.Connection, normalized: str, limit: int) -> list[int]:
        if not self._vector_loaded:
            return []
        total = connection.execute("SELECT count(*) FROM memory_documents").fetchone()
        if total is None or int(total[0]) == 0:
            return []
        module = _load_sqlite_vec()
        if module is None:
            return []
        try:
            rows = connection.execute(
                "SELECT id FROM memory_vec WHERE embedding MATCH ? AND k = ?",
                (module.serialize_float32(embed(normalized)), limit),
            ).fetchall()
        except sqlite3.Error:
            return []
        return [int(row[0]) for row in rows]

    def _hits(self, ordered: list[int], fts_ids: set[int], tokens: list[str]) -> list[MemoryHit]:
        connection = self._connect()
        try:
            with connection:
                hits: list[MemoryHit] = []
                for rank, doc_id in enumerate(ordered, start=1):
                    row = connection.execute(
                        "SELECT source, ref, title, body FROM memory_documents WHERE id = ?",
                        (doc_id,),
                    ).fetchone()
                    if row is None:
                        continue
                    body = str(row[3])
                    snippet = (
                        self._fts_snippet(connection, doc_id)
                        if doc_id in fts_ids and tokens
                        else self._body_snippet(body, tokens)
                    )
                    hits.append(
                        MemoryHit(
                            source=str(row[0]),
                            ref=str(row[1]),
                            title=str(row[2]),
                            snippet=snippet,
                            score=round(1.0 / (_RRF_K + rank), 6),
                        )
                    )
                return hits
        finally:
            connection.close()

    def _fts_snippet(self, connection: sqlite3.Connection, doc_id: int) -> str:
        try:
            row = connection.execute(
                "SELECT snippet(memory_fts, 1, '', '', ' ... ', 24) "
                "FROM memory_fts WHERE rowid = ?",
                (doc_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            return ""
        return str(row[0])[:_MAX_SNIPPET] if row is not None else ""

    @staticmethod
    def _body_snippet(body: str, tokens: list[str]) -> str:
        lowered = body.lower()
        for token in tokens:
            position = lowered.find(token)
            if position >= 0:
                start = max(0, position - 60)
                return " ".join(body[start : position + 160].split())[:_MAX_SNIPPET]
        return body[:_MAX_SNIPPET]


def terminal_task_documents(store: object, owner_id: str) -> list[tuple[str, str]]:
    """Bounded owner-scoped terminal task records for recall indexing."""
    if not isinstance(store, TaskStore):
        return []
    return [(view.id, view.objective) for view in store.list_terminal(owner_id, limit=_MAX_TASKS)]
