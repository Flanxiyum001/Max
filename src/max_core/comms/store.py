"""Contacts allowlist and inbound inbox (SQLite, no secrets stored)."""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from max_core.tools import ToolError

_PHONE = re.compile(r"\+[1-9][0-9]{7,14}")
_EMAIL = re.compile(r"[^@\s]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,24}")
_NAME = re.compile(r"[\w .'-]{1,60}")
MAX_INBOX = 2_000
BODY_CHARS = 4_000


@dataclass(frozen=True, slots=True)
class Contact:
    name: str
    phone: str
    email: str


@dataclass(frozen=True, slots=True)
class InboundItem:
    id: int
    at: str
    channel: str
    sender: str
    body: str
    read: bool


def normalize_phone(value: str) -> str:
    digits = re.sub(r"[\s()-]", "", value or "")
    if digits and not digits.startswith("+"):
        digits = "+91" + digits.lstrip("0") if len(digits.lstrip("0")) == 10 else "+" + digits
    if digits and not _PHONE.fullmatch(digits):
        raise ToolError("invalid_phone")
    return digits


class CommsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS contacts(
                  name TEXT PRIMARY KEY COLLATE NOCASE, phone TEXT NOT NULL DEFAULT '',
                  email TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS inbox(
                  id INTEGER PRIMARY KEY, at TEXT NOT NULL, channel TEXT NOT NULL,
                  sender TEXT NOT NULL, body TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0,
                  provider_id TEXT UNIQUE);
                CREATE TABLE IF NOT EXISTS sent(
                  key TEXT PRIMARY KEY, at TEXT NOT NULL, channel TEXT NOT NULL,
                  contact TEXT NOT NULL, provider_id TEXT NOT NULL DEFAULT '');
                """
            )

    def _db(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    # contacts ----------------------------------------------------------------
    def add_contact(self, name: str, phone: str = "", email: str = "") -> Contact:
        name = " ".join(name.split())
        if not _NAME.fullmatch(name):
            raise ToolError("invalid_contact_name")
        phone = normalize_phone(phone)
        email = email.strip()
        if email and not _EMAIL.fullmatch(email):
            raise ToolError("invalid_email")
        if not phone and not email:
            raise ToolError("contact_needs_phone_or_email")
        with self._lock, self._db() as db:
            db.execute(
                "INSERT INTO contacts(name, phone, email) VALUES (?,?,?) ON CONFLICT(name) "
                "DO UPDATE SET phone=excluded.phone, email=excluded.email",
                (name, phone, email),
            )
        return Contact(name, phone, email)

    def remove_contact(self, name: str) -> None:
        with self._lock, self._db() as db:
            if db.execute("DELETE FROM contacts WHERE name=?", (name.strip(),)).rowcount == 0:
                raise ToolError("unknown_contact")

    def contact(self, name: str) -> Contact:
        with self._db() as db:
            row = db.execute(
                "SELECT name, phone, email FROM contacts WHERE name=?", (name.strip(),)
            ).fetchone()
        if row is None:
            raise ToolError("unknown_contact")
        return Contact(*row)

    def contacts(self) -> list[Contact]:
        with self._db() as db:
            return [Contact(*r) for r in db.execute("SELECT name, phone, email FROM contacts")]

    def name_for(self, address: str) -> str | None:
        with self._db() as db:
            row = db.execute(
                "SELECT name FROM contacts WHERE phone=? OR email=? COLLATE NOCASE",
                (address, address),
            ).fetchone()
        return row[0] if row else None

    # inbox -------------------------------------------------------------------
    def receive(self, channel: str, sender: str, body: str, provider_id: str | None) -> bool:
        """Store one inbound item; False if it was a duplicate (webhook retry)."""
        with self._lock, self._db() as db:
            try:
                db.execute(
                    "INSERT INTO inbox(at, channel, sender, body, provider_id) VALUES (?,?,?,?,?)",
                    (
                        datetime.now(UTC).isoformat(timespec="seconds"),
                        channel,
                        sender[:200],
                        body[:BODY_CHARS],
                        provider_id,
                    ),
                )
            except sqlite3.IntegrityError:
                return False
            db.execute(
                "DELETE FROM inbox WHERE id <= (SELECT MAX(id) FROM inbox) - ?", (MAX_INBOX,)
            )
        return True

    def inbox(
        self, channel: str | None = None, unread_only: bool = True, limit: int = 10
    ) -> list[InboundItem]:
        query = "SELECT id, at, channel, sender, body, read FROM inbox WHERE 1=1"
        params: list[object] = []
        if channel:
            query += " AND channel=?"
            params.append(channel)
        if unread_only:
            query += " AND read=0"
        query += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 50)))
        with self._lock, self._db() as db:
            rows = db.execute(query, params).fetchall()
            if rows:
                db.execute(
                    f"UPDATE inbox SET read=1 WHERE id IN ({','.join('?' * len(rows))})",  # noqa: S608
                    [r[0] for r in rows],
                )
        return [InboundItem(r[0], r[1], r[2], r[3], r[4], bool(r[5])) for r in rows]

    # idempotency for sends ---------------------------------------------------
    def already_sent(self, key: str) -> bool:
        with self._db() as db:
            return db.execute("SELECT 1 FROM sent WHERE key=?", (key,)).fetchone() is not None

    def mark_sent(self, key: str, channel: str, contact: str, provider_id: str) -> None:
        with self._lock, self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO sent(key, at, channel, contact, provider_id) "
                "VALUES (?,?,?,?,?)",
                (
                    key,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    channel,
                    contact,
                    provider_id,
                ),
            )
