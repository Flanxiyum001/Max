"""Transactional task state, checkpoints and a *non-dispatching* durable outbox.

No action here runs arbitrary commands, invokes an AI model, or performs an external effect.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Engine, create_engine, or_, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from max_core.models import OutboxEvent, Task, TaskCheckpoint, utc_now

_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,100}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


class StoreError(Exception):
    """Client-visible reason without private SQL or credential material."""

    def __init__(self, reason: str, status_code: int = 409) -> None:
        self.reason = reason
        self.status_code = status_code
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class TaskView:
    id: str
    objective: str
    state: str
    revision: int
    created_at: datetime
    updated_at: datetime
    last_checkpoint_id: str | None

    @classmethod
    def from_record(cls, task: Task) -> TaskView:
        return cls(
            id=task.id,
            objective=task.objective,
            state=task.state,
            revision=task.revision,
            created_at=task.created_at,
            updated_at=task.updated_at,
            last_checkpoint_id=task.last_checkpoint_id,
        )


@dataclass(frozen=True, slots=True)
class OutboxLease:
    id: str
    task_id: str
    event_key: str
    kind: str
    task_revision: int
    claim_token: str


def _validate_key(key: str) -> None:
    if not _KEY.fullmatch(key):
        raise StoreError("Invalid idempotency key", 422)


def _validate_digest(digest: str) -> None:
    if not _DIGEST.fullmatch(digest):
        raise StoreError("Invalid SHA-256 digest", 422)


def _digest(objective: str) -> str:
    return hashlib.sha256(objective.encode("utf-8")).hexdigest()


def _locked(session: Session, task_id: str, owner_id: str) -> Task:
    task = session.scalar(
        select(Task).where(Task.id == task_id, Task.owner_id == owner_id).with_for_update()
    )
    if task is None:
        raise StoreError("Task not found", 404)
    return task


def _event(session: Session, task: Task, kind: str) -> None:
    session.add(
        OutboxEvent(
            id=str(uuid4()),
            task_id=task.id,
            event_key=f"{task.id}:{task.revision}:{kind}",
            kind=kind,
            task_revision=task.revision,
        )
    )


def _advance(task: Task, state: str) -> None:
    task.state = state
    task.revision += 1
    task.updated_at = utc_now()


def _is_expired(lease_until: datetime | None) -> bool:
    if lease_until is None:
        return True
    instant = lease_until if lease_until.tzinfo is not None else lease_until.replace(tzinfo=UTC)
    return instant.astimezone(UTC) <= utc_now()


class TaskStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.sessions = sessionmaker(engine, expire_on_commit=False)

    def ready(self) -> bool:
        """Check both connectivity and expected migrated schema, not just SELECT 1."""
        try:
            with self.engine.connect() as connection:
                version = connection.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
                if version != "0003_independent_observer":
                    return False
                connection.execute(text("SELECT id FROM max_tasks LIMIT 1"))
                connection.execute(text("SELECT id FROM max_approval_grants LIMIT 1"))
                connection.execute(text("SELECT id FROM max_trust_audit LIMIT 1"))
                connection.execute(text("SELECT id FROM max_verification_checks LIMIT 1"))
            return True
        except SQLAlchemyError:
            return False

    def create(self, owner_id: str, objective: str, idempotency_key: str) -> tuple[TaskView, bool]:
        _validate_key(idempotency_key)
        normalized = objective.strip()
        if not 1 <= len(normalized) <= 500:
            raise StoreError("Objective must be between 1 and 500 characters", 422)
        fingerprint = _digest(normalized)
        task = Task(
            id=str(uuid4()),
            owner_id=owner_id,
            objective=normalized,
            objective_hash=fingerprint,
            idempotency_key=idempotency_key,
            state="queued",
            revision=1,
        )
        try:
            with self.sessions.begin() as session:
                session.add(task)
                session.flush()  # task FK must exist before inserting an outbox record
                _event(session, task, "task.queued")
                session.flush()
            return TaskView.from_record(task), True
        except IntegrityError:
            # A competing request committed first. Retrieve *only* this owner's key,
            # then reject conflicting payloads instead of silently returning another task.
            with self.sessions() as session:
                existing = session.scalar(
                    select(Task).where(
                        Task.owner_id == owner_id, Task.idempotency_key == idempotency_key
                    )
                )
                if existing is None:
                    raise StoreError("Persistence conflict") from None
                if existing.objective_hash != fingerprint:
                    raise StoreError("Idempotency key already used for another objective") from None
                return TaskView.from_record(existing), False

    def get(self, owner_id: str, task_id: str) -> TaskView:
        with self.sessions() as session:
            task = session.scalar(select(Task).where(Task.id == task_id, Task.owner_id == owner_id))
            if task is None:
                raise StoreError("Task not found", 404)
            return TaskView.from_record(task)

    def list_recent(self, owner_id: str, limit: int = 50) -> list[TaskView]:
        with self.sessions() as session:
            records = session.scalars(
                select(Task)
                .where(Task.owner_id == owner_id)
                .order_by(Task.created_at.desc(), Task.id.desc())
                .limit(limit)
            ).all()
            return [TaskView.from_record(record) for record in records]

    def cancel(self, owner_id: str, task_id: str) -> TaskView:
        with self.sessions.begin() as session:
            task = _locked(session, task_id, owner_id)
            if task.state == "queued":
                _advance(task, "cancelled")
                _event(session, task, "task.cancelled")
            elif task.state == "running":
                _advance(task, "cancelling")
                _event(session, task, "task.cancel_requested")
            elif task.state not in {"cancelled", "cancelling"}:
                raise StoreError("Task cannot be cancelled in this state")
            session.flush()
            return TaskView.from_record(task)

    def claim(self, owner_id: str, task_id: str, worker_id: str, ttl_seconds: int = 60) -> TaskView:
        """Internal worker API; never expose a claim endpoint to model/client."""
        if not 1 <= len(worker_id) <= 100 or not 5 <= ttl_seconds <= 3600:
            raise StoreError("Invalid worker lease", 422)
        with self.sessions.begin() as session:
            task = _locked(session, task_id, owner_id)
            if task.state != "queued":
                raise StoreError("Task is not queued")
            _advance(task, "running")
            task.lease_owner = worker_id
            task.lease_until = utc_now() + timedelta(seconds=ttl_seconds)
            _event(session, task, "task.running")
            session.flush()
            return TaskView.from_record(task)

    def checkpoint(
        self,
        owner_id: str,
        task_id: str,
        worker_id: str,
        operation_key: str,
        step_name: str,
        payload_digest: str,
    ) -> TaskView:
        _validate_key(operation_key)
        _validate_digest(payload_digest)
        if not 1 <= len(step_name) <= 100:
            raise StoreError("Invalid step name", 422)
        with self.sessions.begin() as session:
            task = _locked(session, task_id, owner_id)
            if task.lease_owner != worker_id:
                raise StoreError("Worker has no active lease")
            previous = session.scalar(
                select(TaskCheckpoint).where(
                    TaskCheckpoint.task_id == task_id,
                    TaskCheckpoint.operation_key == operation_key,
                )
            )
            if previous is not None:
                if previous.step_name != step_name or previous.payload_digest != payload_digest:
                    raise StoreError("Checkpoint operation key reused for different payload")
                return TaskView.from_record(task)
            if (
                task.state != "running"
                or task.lease_owner != worker_id
                or _is_expired(task.lease_until)
            ):
                raise StoreError("Worker has no active lease")
            cp = TaskCheckpoint(
                id=str(uuid4()),
                task_id=task.id,
                operation_key=operation_key,
                step_name=step_name,
                payload_digest=payload_digest,
            )
            session.add(cp)
            task.last_checkpoint_id = cp.id
            _advance(task, "running")
            _event(session, task, "task.checkpointed")
            session.flush()
            return TaskView.from_record(task)

    def record_outcome(
        self, owner_id: str, task_id: str, worker_id: str, evidence_digest: str
    ) -> TaskView:
        """A worker observation is *not* independent verification or completion."""
        _validate_digest(evidence_digest)
        with self.sessions.begin() as session:
            task = _locked(session, task_id, owner_id)
            if (
                task.state != "running"
                or task.lease_owner != worker_id
                or _is_expired(task.lease_until)
            ):
                raise StoreError("Worker has no active lease")
            _advance(task, "awaiting_verification")
            task.evidence_digest = evidence_digest
            task.lease_owner = None
            task.lease_until = None
            _event(session, task, "task.awaiting_verification")
            session.flush()
            return TaskView.from_record(task)

    def reconcile_expired(self, owner_id: str, limit: int = 50) -> int:
        """Stale running/cancelling work is quarantined, never blindly replayed."""
        with self.sessions.begin() as session:
            stmt = (
                select(Task)
                .where(Task.owner_id == owner_id)
                .where(Task.state.in_(["running", "cancelling"]))
                .where(Task.lease_until < utc_now())
                .order_by(Task.updated_at, Task.id)
                .limit(limit)
            )
            if self.engine.dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            tasks = session.scalars(stmt).all()
            for task in tasks:
                _advance(task, "needs_reconciliation")
                task.lease_owner = None
                task.lease_until = None
                _event(session, task, "task.needs_reconciliation")
            return len(tasks)

    def acknowledge_stopped_cancel(self, owner_id: str, task_id: str, worker_id: str) -> TaskView:
        """Trusted worker only: finish cancellation AFTER its local work has stopped.

        A client/model cannot call this method through the HTTP API. Expired or
        mismatched leases must be reconciled rather than treated as confirmation.
        """
        with self.sessions.begin() as session:
            task = _locked(session, task_id, owner_id)
            if (
                task.state != "cancelling"
                or task.lease_owner != worker_id
                or _is_expired(task.lease_until)
            ):
                raise StoreError("No active cancellation acknowledgment lease")
            _advance(task, "cancelled")
            task.lease_owner = None
            task.lease_until = None
            _event(session, task, "task.cancelled")
            session.flush()
            return TaskView.from_record(task)

    def quarantine_claim(self, owner_id: str, task_id: str, worker_id: str) -> TaskView:
        """Trusted worker: uncertain execution => reconciliation, NEVER replay.

        Only the current unexpired lease owner may quarantine running/cancelling
        work. No success assertion, verified completion or automatic retry.
        """
        with self.sessions.begin() as session:
            task = _locked(session, task_id, owner_id)
            if (
                task.state not in {"running", "cancelling"}
                or task.lease_owner != worker_id
                or _is_expired(task.lease_until)
            ):
                raise StoreError("No active worker lease for quarantine")
            _advance(task, "needs_reconciliation")
            task.lease_owner = None
            task.lease_until = None
            _event(session, task, "task.needs_reconciliation")
            session.flush()
            return TaskView.from_record(task)

    def lease_outbox(self, limit: int = 20, ttl_seconds: int = 60) -> list[OutboxLease]:
        """Claims metadata events, not external actions. Re-leasing needs idempotent sink."""
        if not 1 <= limit <= 100 or not 5 <= ttl_seconds <= 3600:
            raise StoreError("Invalid outbox lease", 422)
        with self.sessions.begin() as session:
            stmt = (
                select(OutboxEvent)
                .where(
                    or_(
                        OutboxEvent.delivery_state == "pending",
                        (OutboxEvent.delivery_state == "leased")
                        & (OutboxEvent.lease_until < utc_now()),
                    )
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(limit)
            )
            if self.engine.dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            events = session.scalars(stmt).all()
            result = []
            for event in events:
                token = str(uuid4())
                event.delivery_state = "leased"
                event.claim_token = token
                event.lease_until = utc_now() + timedelta(seconds=ttl_seconds)
                event.attempts += 1
                result.append(
                    OutboxLease(
                        event.id,
                        event.task_id,
                        event.event_key,
                        event.kind,
                        event.task_revision,
                        token,
                    )
                )
            return result

    def ack_outbox(self, event_id: str, claim_token: str) -> bool:
        with self.sessions.begin() as session:
            event = session.scalar(
                select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
            )
            if event is None:
                raise StoreError("Event not found", 404)
            if (
                event.delivery_state != "leased"
                or event.claim_token != claim_token
                or _is_expired(event.lease_until)
            ):
                return False
            event.delivery_state = "delivered"
            event.delivered_at = utc_now()
            event.lease_until = None
            event.claim_token = None
            return True


def create_store(url: str) -> TaskStore:
    """Build a production store for PostgreSQL; unit tests inject SQLite engines directly."""
    if not url.startswith("postgresql+psycopg://"):
        raise StoreError("Only PostgreSQL+psycopg storage is allowed in the running API", 422)
    engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)
    return TaskStore(engine)
