"""Owner-scoped, review-first memory foundation. No model or network integration.

Only a trusted host may call write methods with an authenticated owner identity.
Suggested memories never become retrieval facts until that owner approves them.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Engine, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from max_core.models import MemoryEvent, MemoryRecord, utc_now

_OWNER = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_SCOPE = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")
_KEY = re.compile(r"[a-z][a-z0-9_.-]{2,95}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SOURCES = frozenset({"explicit_user_correction", "inferred_from_task", "external_doc"})
_HALF_LIFE_DAYS = {"inferred_from_task": 30, "external_doc": 90}
_DEFAULT_RETENTION_DAYS = {"inferred_from_task": 90, "external_doc": 365}


class MemoryStoreError(Exception):
    """Fixed operational codes: never include memory contents or DB errors."""


@dataclass(frozen=True, slots=True, repr=False)
class MemoryView:
    id: str
    scope: str
    subject_key: str
    content: str
    source_kind: str
    source_sha256: str
    confidence: int
    state: str
    revision: int
    observed_at: datetime
    updated_at: datetime
    expires_at: datetime | None

    @classmethod
    def from_record(cls, row: MemoryRecord) -> MemoryView:
        return cls(
            id=row.id,
            scope=row.scope,
            subject_key=row.subject_key,
            content=row.content,
            source_kind=row.source_kind,
            source_sha256=row.source_sha256,
            confidence=row.confidence,
            state=row.state,
            revision=row.revision,
            observed_at=_utc(row.observed_at),
            updated_at=_utc(row.updated_at),
            expires_at=_utc(row.expires_at) if row.expires_at is not None else None,
        )


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    memory: MemoryView
    effective_confidence: int


@dataclass(frozen=True, slots=True)
class MemoryAuditView:
    action: str
    revision: int
    created_at: datetime


def _utc(value: datetime) -> datetime:
    """SQLite drops tzinfo; all stored application timestamps are UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _clock(now: datetime | None) -> datetime:
    if now is None:
        return utc_now()
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise MemoryStoreError("invalid_timestamp")
    return now.astimezone(UTC)


def _scope_args(owner_id: str, scope: str) -> None:
    if not isinstance(owner_id, str) or _OWNER.fullmatch(owner_id) is None:
        raise MemoryStoreError("invalid_owner")
    if not isinstance(scope, str) or _SCOPE.fullmatch(scope) is None:
        raise MemoryStoreError("invalid_scope")


def _content_args(
    key: str, content: str, source_kind: str, source_sha256: str, confidence: int
) -> None:
    if not isinstance(key, str) or _KEY.fullmatch(key) is None:
        raise MemoryStoreError("invalid_subject_key")
    if not isinstance(content, str) or not 1 <= len(content.strip()) <= 2000:
        raise MemoryStoreError("invalid_memory_content")
    if source_kind not in _SOURCES:
        raise MemoryStoreError("invalid_memory_source")
    if not isinstance(source_sha256, str) or _DIGEST.fullmatch(source_sha256) is None:
        raise MemoryStoreError("invalid_source_digest")
    if type(confidence) is not int or not 0 <= confidence <= 1000:
        raise MemoryStoreError("invalid_memory_confidence")


def _event(session: Session, record: MemoryRecord, action: str, at: datetime) -> None:
    # Audit contains no text, prompt, source URL, embedding, or previous value.
    session.add(
        MemoryEvent(
            id=str(uuid4()),
            memory_id=record.id,
            owner_id=record.owner_id,
            action=action,
            revision=record.revision,
            created_at=at,
        )
    )


def _locked(session: Session, owner_id: str, memory_id: str) -> MemoryRecord:
    row = session.scalar(
        select(MemoryRecord)
        .where(MemoryRecord.id == memory_id, MemoryRecord.owner_id == owner_id)
        .with_for_update()
    )
    if row is None:
        raise MemoryStoreError("memory_not_found")
    return row


def _effective(record: MemoryRecord, at: datetime) -> int:
    if record.state != "approved":
        return 0
    if record.expires_at is not None and _utc(record.expires_at) <= at:
        return 0
    half_life = _HALF_LIFE_DAYS.get(record.source_kind)
    if half_life is None:
        return record.confidence
    age_days = max(0.0, (at - _utc(record.observed_at)).total_seconds() / 86400)
    return max(0, min(1000, math.floor(record.confidence * 2 ** (-age_days / half_life))))


class MemoryStore:
    """SQL-only first slice; no embedding, automatic learning, API, or background jobs."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.sessions = sessionmaker(engine, expire_on_commit=False)

    def propose(
        self,
        *,
        owner_id: str,
        scope: str,
        subject_key: str,
        content: str,
        source_kind: str,
        source_sha256: str,
        confidence: int,
        observed_at: datetime | None = None,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> MemoryView:
        """Stage an untrusted candidate, never an approved fact.

        Caller must classify and exclude secrets/sensitive data before invoking.
        One memory slot per owner/scope/key prevents silent contradictions.
        """
        _scope_args(owner_id, scope)
        _content_args(subject_key, content, source_kind, source_sha256, confidence)
        at = _clock(now)
        observed = _clock(observed_at) if observed_at is not None else at
        if observed > at + timedelta(minutes=5):
            raise MemoryStoreError("future_memory_observation")
        expiry = _clock(expires_at) if expires_at is not None else None
        if expiry is None and source_kind in _DEFAULT_RETENTION_DAYS:
            expiry = observed + timedelta(days=_DEFAULT_RETENTION_DAYS[source_kind])
        if expiry is not None and expiry <= at:
            raise MemoryStoreError("expired_memory_proposal")
        record = MemoryRecord(
            id=str(uuid4()),
            owner_id=owner_id,
            scope=scope,
            subject_key=subject_key,
            content=content.strip(),
            source_kind=source_kind,
            source_sha256=source_sha256,
            confidence=confidence,
            state="pending",
            revision=1,
            created_at=at,
            updated_at=at,
            observed_at=observed,
            expires_at=expiry,
        )
        try:
            with self.sessions.begin() as session:
                session.add(record)
                session.flush()
                _event(session, record, "proposed", at)
            return MemoryView.from_record(record)
        except IntegrityError:
            raise MemoryStoreError("memory_slot_exists") from None

    def review(
        self,
        owner_id: str,
        memory_id: str,
        *,
        expected_revision: int,
        approve: bool,
        now: datetime | None = None,
    ) -> MemoryView:
        if not isinstance(owner_id, str) or _OWNER.fullmatch(owner_id) is None:
            raise MemoryStoreError("invalid_owner")
        if type(approve) is not bool or type(expected_revision) is not int:
            raise MemoryStoreError("invalid_review")
        at = _clock(now)
        with self.sessions.begin() as session:
            record = _locked(session, owner_id, memory_id)
            if record.revision != expected_revision or record.state != "pending":
                raise MemoryStoreError("stale_memory_review")
            if approve and record.expires_at is not None and _utc(record.expires_at) <= at:
                raise MemoryStoreError("expired_memory_proposal")
            record.state = "approved" if approve else "rejected"
            record.revision += 1
            record.updated_at = at
            _event(session, record, record.state, at)
            session.flush()
            return MemoryView.from_record(record)

    def correct(
        self,
        owner_id: str,
        memory_id: str,
        *,
        expected_revision: int,
        content: str,
        source_sha256: str,
        now: datetime | None = None,
    ) -> MemoryView:
        """Trusted owner correction; never called directly from a model proposal."""
        if not isinstance(owner_id, str) or _OWNER.fullmatch(owner_id) is None:
            raise MemoryStoreError("invalid_owner")
        _content_args("correction", content, "explicit_user_correction", source_sha256, 1000)
        if type(expected_revision) is not int:
            raise MemoryStoreError("invalid_revision")
        at = _clock(now)
        with self.sessions.begin() as session:
            record = _locked(session, owner_id, memory_id)
            if record.revision != expected_revision or record.state != "approved":
                raise MemoryStoreError("stale_memory_correction")
            # Previous text is overwritten, not duplicated into audit history.
            record.content = content.strip()
            record.source_kind = "explicit_user_correction"
            record.source_sha256 = source_sha256
            record.confidence = 1000
            record.observed_at = at
            record.expires_at = None
            record.updated_at = at
            record.revision += 1
            _event(session, record, "corrected", at)
            session.flush()
            return MemoryView.from_record(record)

    def delete(self, owner_id: str, memory_id: str, *, expected_revision: int) -> None:
        """Hard erase candidate, its provenance and all associated audit metadata."""
        if not isinstance(owner_id, str) or _OWNER.fullmatch(owner_id) is None:
            raise MemoryStoreError("invalid_owner")
        if type(expected_revision) is not int:
            raise MemoryStoreError("invalid_revision")
        with self.sessions.begin() as session:
            record = _locked(session, owner_id, memory_id)
            if record.revision != expected_revision:
                raise MemoryStoreError("stale_memory_delete")
            session.execute(delete(MemoryEvent).where(MemoryEvent.memory_id == record.id))
            session.delete(record)

    def review_queue(
        self,
        owner_id: str,
        *,
        scope: str,
        days: int = 7,
        limit: int = 5,
        now: datetime | None = None,
    ) -> tuple[MemoryView, ...]:
        _scope_args(owner_id, scope)
        if type(days) is not int or not 1 <= days <= 31:
            raise MemoryStoreError("invalid_review_period")
        if type(limit) is not int or not 1 <= limit <= 5:
            raise MemoryStoreError("invalid_review_limit")
        at = _clock(now)
        with self.sessions() as session:
            rows = session.scalars(
                select(MemoryRecord)
                .where(
                    MemoryRecord.owner_id == owner_id,
                    MemoryRecord.scope == scope,
                    MemoryRecord.state == "pending",
                    MemoryRecord.created_at >= at - timedelta(days=days),
                    (MemoryRecord.expires_at.is_(None)) | (MemoryRecord.expires_at > at),
                )
                .order_by(MemoryRecord.created_at.desc(), MemoryRecord.id)
                .limit(limit)
            ).all()
            return tuple(MemoryView.from_record(row) for row in rows)

    def retrieve(
        self,
        owner_id: str,
        *,
        scope: str,
        minimum_confidence: int = 400,
        limit: int = 20,
        now: datetime | None = None,
    ) -> tuple[RetrievedMemory, ...]:
        """Owner/scope bounded approved SQL retrieval. No semantic search yet."""
        _scope_args(owner_id, scope)
        if type(minimum_confidence) is not int or not 0 <= minimum_confidence <= 1000:
            raise MemoryStoreError("invalid_confidence_filter")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise MemoryStoreError("invalid_retrieval_limit")
        at = _clock(now)
        with self.sessions() as session:
            rows = session.scalars(
                select(MemoryRecord)
                .where(
                    MemoryRecord.owner_id == owner_id,
                    MemoryRecord.scope == scope,
                    MemoryRecord.state == "approved",
                    (MemoryRecord.expires_at.is_(None)) | (MemoryRecord.expires_at > at),
                )
                .order_by(MemoryRecord.updated_at.desc(), MemoryRecord.id)
                .limit(1000)
            ).all()
            selected = [
                RetrievedMemory(MemoryView.from_record(row), _effective(row, at)) for row in rows
            ]
            selected = [
                item for item in selected if item.effective_confidence >= minimum_confidence
            ]
            selected.sort(key=lambda item: (-item.effective_confidence, item.memory.subject_key))
            return tuple(selected[:limit])

    def audit(self, owner_id: str, memory_id: str) -> tuple[MemoryAuditView, ...]:
        if not isinstance(owner_id, str) or _OWNER.fullmatch(owner_id) is None:
            raise MemoryStoreError("invalid_owner")
        with self.sessions() as session:
            # Do not leak even the presence of another owner's memory or events.
            row = session.scalar(
                select(MemoryRecord.id).where(
                    MemoryRecord.id == memory_id, MemoryRecord.owner_id == owner_id
                )
            )
            if row is None:
                raise MemoryStoreError("memory_not_found")
            events = session.scalars(
                select(MemoryEvent)
                .where(MemoryEvent.memory_id == memory_id, MemoryEvent.owner_id == owner_id)
                .order_by(MemoryEvent.revision)
            ).all()
            return tuple(
                MemoryAuditView(event.action, event.revision, _utc(event.created_at))
                for event in events
            )
