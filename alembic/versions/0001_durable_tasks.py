"""Create durable task, checkpoint and transactional outbox tables.

Revision ID: 0001_durable_tasks
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0001_durable_tasks"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "max_tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("objective", sa.String(500), nullable=False),
        sa.Column("objective_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(100), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checkpoint_id", sa.String(36), nullable=True),
        sa.Column("evidence_digest", sa.String(64), nullable=True),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_task_create_key"),
    )
    op.create_table(
        "max_task_checkpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("max_tasks.id"), nullable=False),
        sa.Column("operation_key", sa.String(100), nullable=False),
        sa.Column("step_name", sa.String(100), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "operation_key", name="uq_checkpoint_operation"),
    )
    op.create_index("ix_max_task_checkpoints_task_id", "max_task_checkpoints", ["task_id"])
    op.create_table(
        "max_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("max_tasks.id"), nullable=False),
        sa.Column("event_key", sa.String(150), nullable=False, unique=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("task_revision", sa.Integer(), nullable=False),
        sa.Column("delivery_state", sa.String(16), nullable=False),
        sa.Column("claim_token", sa.String(36), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_max_outbox_task_id", "max_outbox", ["task_id"])
    op.create_index("ix_max_outbox_lease", "max_outbox", ["delivery_state", "lease_until"])


def downgrade() -> None:
    op.drop_index("ix_max_outbox_lease", table_name="max_outbox")
    op.drop_index("ix_max_outbox_task_id", table_name="max_outbox")
    op.drop_table("max_outbox")
    op.drop_index("ix_max_task_checkpoints_task_id", table_name="max_task_checkpoints")
    op.drop_table("max_task_checkpoints")
    op.drop_table("max_tasks")
