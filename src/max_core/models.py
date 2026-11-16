"""SQLAlchemy-owned durable state. DB schema is changed only through Alembic."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
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
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_id: Mapped[str] = mapped_column(String(128), nullable=False)
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
    __table_args__ = (
        Index("ix_max_verification_task", "task_id", "checked_at"),
        UniqueConstraint(
            "owner_id", "task_id", "request_id", name="uq_observer_owner_task_request"
        ),
        # A request ID is owner-global; cross-task reuse must conflict, not silently fork.
        UniqueConstraint("owner_id", "request_id", name="uq_observer_owner_request"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str] = mapped_column(ForeignKey("max_tasks.id"), nullable=False)
    observed_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    verdict: Mapped[str] = mapped_column(String(30), nullable=False)
    # Nullable only for backward compatibility with pre-Milestone 8 checks.
    request_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checkpoint_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    input_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    observer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    observer_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_kind: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ConnectDraft(Base):
    """One inert review suggestion; independent from executable task state."""

    __tablename__ = "max_connect_drafts"
    __table_args__ = (
        UniqueConstraint("owner_id", "source", "event_key", name="uq_connect_source_event"),
        Index("ix_connect_owner_state_created", "owner_id", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    event_key: Mapped[str] = mapped_column(String(100), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    draft: Mapped[str] = mapped_column(String(500), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryRecord(Base):
    """Owner-scoped review candidate or approved fact, not a model instruction."""

    __tablename__ = "max_memories"
    __table_args__ = (
        UniqueConstraint("owner_id", "scope", "subject_key", name="uq_memory_owner_scope_key"),
        Index("ix_max_memories_lookup", "owner_id", "scope", "state", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_key: Mapped[str] = mapped_column(String(96), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryEvent(Base):
    """Metadata-only revision audit. Deleted memories erase their audit metadata."""

    __tablename__ = "max_memory_events"
    __table_args__ = (Index("ix_max_memory_events_owner", "owner_id", "memory_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    memory_id: Mapped[str] = mapped_column(
        ForeignKey("max_memories.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeviceNode(Base):
    """Owner-registered public key, not proof of physical hardware provenance."""

    __tablename__ = "max_device_nodes"
    __table_args__ = (
        UniqueConstraint("owner_id", "key_sha256", name="uq_node_owner_key"),
        Index("ix_nodes_owner_state", "owner_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    public_key_b64url: Mapped[str] = mapped_column(String(512), nullable=False)
    key_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DeviceChallenge(Base):
    """One-use hashed challenge, bound to owner, node and purpose."""

    __tablename__ = "max_device_challenges"
    __table_args__ = (
        Index("ix_node_challenges_device", "owner_id", "device_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("max_device_nodes.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    nonce_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
