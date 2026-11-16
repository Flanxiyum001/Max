"""Local authenticated event intake producing inert, persistent review drafts.

No connectors, remote webhook listeners, model calls, tool dispatch, or task
execution run here. Event metadata is a claim supplied by a trusted local bridge,
not verified proof that an external event occurred.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from max_core.models import ConnectDraft, utc_now
from max_core.store import StoreError

_EVENT_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")
_RESOURCE_KEY = re.compile(r"^[A-Za-z0-9._:/#-]{1,80}$")
_MAX_PENDING = 50
_TEMPLATES = {
    ("github_ci", "ci.failed"): (
        "Review failed CI",
        "CI failure reported for {resource}. Inspect the run and prepare a repair "
        "proposal in an isolated workspace. Do not commit, publish or rerun without approval.",
    ),
    ("calendar", "calendar.upcoming"): (
        "Prepare for calendar event",
        "Upcoming event reported for {resource}. Review the agenda and prepare "
        "a private checklist. Do not contact attendees or edit the calendar.",
    ),
    ("local_build", "build.finished"): (
        "Review local build result",
        "Local build completion reported for {resource}. Inspect trusted build "
        "evidence and prepare a summary. Do not execute new commands or deploy.",
    ),
}


def _as_utc(value: datetime) -> datetime:
    """Normalize SQLite-naive and PostgreSQL-aware timestamps for stable API replies."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class DraftView:
    id: str
    source: str
    kind: str
    resource_key: str
    title: str
    draft: str
    state: str
    created_at: datetime
    reviewed_at: datetime | None

    @classmethod
    def from_record(cls, record: ConnectDraft) -> DraftView:
        return cls(
            id=record.id,
            source=record.source,
            kind=record.kind,
            resource_key=record.resource_key,
            title=record.title,
            draft=record.draft,
            state=record.state,
            created_at=_as_utc(record.created_at),
            reviewed_at=_as_utc(record.reviewed_at) if record.reviewed_at is not None else None,
        )


class ConnectService:
    """Persistent, owner-scoped, idempotent and non-executing suggestion feed."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.sessions = sessionmaker(engine, expire_on_commit=False)

    def ingest(
        self,
        owner_id: str,
        source: str,
        kind: str,
        event_key: str,
        resource_key: str,
    ) -> tuple[DraftView, bool]:
        if (source, kind) not in _TEMPLATES:
            raise StoreError("Unsupported event type", 422)
        if not isinstance(event_key, str) or not _EVENT_KEY.fullmatch(event_key):
            raise StoreError("Invalid event key", 422)
        if not isinstance(resource_key, str) or not _RESOURCE_KEY.fullmatch(resource_key):
            raise StoreError("Invalid resource key", 422)
        fingerprint = hashlib.sha256(
            json.dumps(
                [source, kind, event_key, resource_key],
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        title, template = _TEMPLATES[(source, kind)]
        try:
            with self.sessions.begin() as session:
                # Serialize the owner queue limit across independent PostgreSQL workers.
                if self.engine.dialect.name == "postgresql":
                    session.execute(
                        text("SELECT pg_advisory_xact_lock(hashtext(:owner))"),
                        {"owner": owner_id},
                    )
                existing = session.scalar(
                    select(ConnectDraft).where(
                        ConnectDraft.owner_id == owner_id,
                        ConnectDraft.source == source,
                        ConnectDraft.event_key == event_key,
                    )
                )
                if existing is not None:
                    if existing.fingerprint != fingerprint:
                        raise StoreError("Event key reused for different metadata")
                    return DraftView.from_record(existing), False
                pending = session.scalar(
                    select(func.count())
                    .select_from(ConnectDraft)
                    .where(
                        ConnectDraft.owner_id == owner_id,
                        ConnectDraft.state == "pending",
                    )
                )
                if pending is None or pending >= _MAX_PENDING:
                    raise StoreError("Pending draft limit reached", 429)
                record = ConnectDraft(
                    id=str(uuid4()),
                    owner_id=owner_id,
                    source=source,
                    kind=kind,
                    event_key=event_key,
                    fingerprint=fingerprint,
                    resource_key=resource_key,
                    title=title,
                    draft=template.format(resource=resource_key),
                    state="pending",
                )
                session.add(record)
                session.flush()
                return DraftView.from_record(record), True
        except IntegrityError:
            # Concurrent exact-key replay; do not allow conflicting payload reuse.
            with self.sessions() as session:
                existing = session.scalar(
                    select(ConnectDraft).where(
                        ConnectDraft.owner_id == owner_id,
                        ConnectDraft.source == source,
                        ConnectDraft.event_key == event_key,
                    )
                )
                if existing is None:
                    raise StoreError("Event persistence conflict") from None
                if existing.fingerprint != fingerprint:
                    raise StoreError("Event key reused for different metadata") from None
                return DraftView.from_record(existing), False

    def list_pending(self, owner_id: str, limit: int = 20) -> list[DraftView]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise StoreError("Invalid draft limit", 422)
        with self.sessions() as session:
            rows = session.scalars(
                select(ConnectDraft)
                .where(ConnectDraft.owner_id == owner_id, ConnectDraft.state == "pending")
                .order_by(ConnectDraft.created_at.desc(), ConnectDraft.id.desc())
                .limit(limit)
            ).all()
            return [DraftView.from_record(row) for row in rows]

    def dismiss(self, owner_id: str, draft_id: str) -> DraftView:
        with self.sessions.begin() as session:
            record = session.scalar(
                select(ConnectDraft)
                .where(ConnectDraft.id == draft_id, ConnectDraft.owner_id == owner_id)
                .with_for_update()
            )
            if record is None:
                raise StoreError("Draft not found", 404)
            if record.state == "pending":
                record.state = "dismissed"
                record.reviewed_at = utc_now()
                session.flush()
            return DraftView.from_record(record)
