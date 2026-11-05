"""SQLAlchemy-owned durable state. DB schema is changed only through Alembic."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Task(Base):
    __tablename__ = "max_tasks"
    __table_args__ = (UniqueConstraint("owner_id", "idempotency_key", name="uq_task_create_key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    objective: Mapped[str] = mapped_column(String(500), nullable=False)
    objective_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_checkpoint_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    evidence_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)


class TaskCheckpoint(Base):
    __tablename__ = "max_task_checkpoints"
    __table_args__ = (UniqueConstraint("task_id", "operation_key", name="uq_checkpoint_operation"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("max_tasks.id"), nullable=False, index=True)
    operation_key: Mapped[str] = mapped_column(String(100), nullable=False)
    step_name: Mapped[str] = mapped_column(String(100), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class OutboxEvent(Base):
    __tablename__ = "max_outbox"
    __table_args__ = (Index("ix_max_outbox_lease", "delivery_state", "lease_until"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("max_tasks.id"), nullable=False, index=True)
    event_key: Mapped[str] = mapped_column(String(150), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    task_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    claim_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovalGrant(Base):
    """Owner decision for one bounded, non-effectful tool invocation."""

    __tablename__ = "max_approval_grants"
    __table_args__ = (Index("ix_max_approval_owner_state", "owner_id", "state"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("max_tasks.id"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    binding_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TrustAudit(Base):
    """Metadata-only decision journal; no raw prompts, credentials or tool parameters."""

    __tablename__ = "max_trust_audit"
    __table_args__ = (Index("ix_max_trust_audit_task", "task_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("max_tasks.id"), nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class VerificationCheck(Base):
    """Immutable-in-application record of an independent observation attempt."""

    __tablename__ = "max_verification_checks"
    __table_args__ = (Index("ix_max_verification_task", "task_id", "checked_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("max_tasks.id"), nullable=False)
    observed_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(30), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
