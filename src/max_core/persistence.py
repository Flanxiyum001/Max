"""Shared internal persistence helpers: owner-scoped row locking and expiry checks."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from max_core.models import Task, utc_now


class StoreError(Exception):
    """Client-visible reason without private SQL or credential material."""

    def __init__(self, reason: str, status_code: int = 409) -> None:
        self.reason = reason
        self.status_code = status_code
        super().__init__(reason)


def _locked(session: Session, task_id: str, owner_id: str) -> Task:
    task = session.scalar(
        select(Task).where(Task.id == task_id, Task.owner_id == owner_id).with_for_update()
    )
    if task is None:
        raise StoreError("Task not found", 404)
    return task


def _is_expired(lease_until: datetime | None) -> bool:
    if lease_until is None:
        return True
    instant = lease_until if lease_until.tzinfo is not None else lease_until.replace(tzinfo=UTC)
    return instant.astimezone(UTC) <= utc_now()
