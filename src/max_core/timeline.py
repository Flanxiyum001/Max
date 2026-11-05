"""Authenticated, bounded, read-only timeline of task *metadata* events.

The outbox is an internal journal, NOT proof that an external action occurred.
Never expose objectives, checkpoint payloads, outbox claim tokens, or tool arguments.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, or_, select

from max_core.models import OutboxEvent, Task
from max_core.store import StoreError, TaskStore

_CURSOR_FORMAT = re.compile(r"^[A-Za-z0-9_-]+\.[0-9a-f]{64}$")
_INVALID_CURSOR = "Invalid timeline cursor"


@dataclass(frozen=True, slots=True)
class TaskEventView:
    event_key: str
    kind: str
    task_revision: int
    created_at: datetime

    @classmethod
    def from_record(cls, record: OutboxEvent) -> TaskEventView:
        return cls(
            event_key=record.event_key,
            kind=record.kind,
            task_revision=record.task_revision,
            created_at=record.created_at,
        )


@dataclass(frozen=True, slots=True)
class TimelinePage:
    task_id: str
    snapshot_revision: int
    events: tuple[TaskEventView, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class _Position:
    snapshot: int
    last_revision: int
    last_id: str


class TaskTimeline:
    """Keyset pagination: owner/task-bound HMAC cursor and fixed task-revision snapshot."""

    def __init__(self, store: TaskStore, secret: str) -> None:
        self.store = store
        self._key = hashlib.sha256(
            b"max-task-timeline-cursor-v1\x00" + secret.encode("utf-8")
        ).digest()

    def _encode(self, owner_id: str, task_id: str, position: _Position) -> str:
        body = json.dumps(
            {
                "v": 1,
                "owner": owner_id,
                "task": task_id,
                "snapshot": position.snapshot,
                "last_revision": position.last_revision,
                "last_id": position.last_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        payload = base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")
        signature = hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{payload}.{signature}"

    def _decode(self, cursor: str, owner_id: str, task_id: str) -> _Position:
        if not isinstance(cursor, str) or len(cursor) > 512 or not _CURSOR_FORMAT.fullmatch(cursor):
            raise StoreError(_INVALID_CURSOR, 422)
        payload, signature = cursor.rsplit(".", 1)
        expected = hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise StoreError(_INVALID_CURSOR, 422)
        try:
            raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
            if base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") != payload:
                raise ValueError("Noncanonical base64")
            data = json.loads(raw.decode("utf-8"))
            if (
                not isinstance(data, dict)
                or set(data) != {"v", "owner", "task", "snapshot", "last_revision", "last_id"}
                or type(data["v"]) is not int
                or data["v"] != 1
                or data["owner"] != owner_id
                or data["task"] != task_id
                or type(data["snapshot"]) is not int
                or type(data["last_revision"]) is not int
                or data["snapshot"] < 1
                or not 1 <= data["last_revision"] <= data["snapshot"]
                or not isinstance(data["last_id"], str)
                or not re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    data["last_id"],
                )
            ):
                raise ValueError("Invalid cursor fields")
            return _Position(
                snapshot=data["snapshot"],
                last_revision=data["last_revision"],
                last_id=data["last_id"],
            )
        except (ValueError, UnicodeError, TypeError, KeyError):
            raise StoreError(_INVALID_CURSOR, 422) from None

    def page(
        self,
        owner_id: str,
        task_id: str,
        *,
        limit: int = 20,
        cursor: str | None = None,
    ) -> TimelinePage:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise StoreError("Invalid timeline limit", 422)
        with self.store.sessions() as session:
            # Ownership checked before inspecting a cursor. A foreign task is always 404.
            task = session.execute(
                select(Task.id, Task.revision).where(Task.id == task_id, Task.owner_id == owner_id)
            ).one_or_none()
            if task is None:
                raise StoreError("Task not found", 404)
            if cursor is None:
                position = _Position(snapshot=task.revision, last_revision=0, last_id="")
            else:
                position = self._decode(cursor, owner_id, task_id)
                if position.snapshot > task.revision:
                    raise StoreError(_INVALID_CURSOR, 422)

            # Never touch lease tokens, outbox payloads or task.objective.
            after = or_(
                OutboxEvent.task_revision > position.last_revision,
                and_(
                    OutboxEvent.task_revision == position.last_revision,
                    OutboxEvent.id > position.last_id,
                ),
            )
            entries = session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.task_id == task_id,
                    OutboxEvent.task_revision <= position.snapshot,
                    after,
                )
                .order_by(OutboxEvent.task_revision, OutboxEvent.id)
                .limit(limit + 1)
            ).all()
            has_more = len(entries) > limit
            visible = entries[:limit]
            next_cursor = None
            if has_more and visible:
                final = visible[-1]
                next_cursor = self._encode(
                    owner_id,
                    task_id,
                    _Position(position.snapshot, final.task_revision, final.id),
                )
            return TimelinePage(
                task_id=task_id,
                snapshot_revision=position.snapshot,
                events=tuple(TaskEventView.from_record(event) for event in visible),
                next_cursor=next_cursor,
            )
