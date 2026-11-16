"""Add owner-scoped review-first MAX memories.

Revision ID: 0005_memory_foundation
Revises: 0004_connect_drafts
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0005_memory_foundation"
down_revision: str | None = "0004_connect_drafts"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "max_memories",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(64), nullable=False),
        sa.Column("subject_key", sa.String(96), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("owner_id", "scope", "subject_key", name="uq_memory_owner_scope_key"),
        sa.CheckConstraint(
            "source_kind IN ('explicit_user_correction', 'inferred_from_task', 'external_doc')",
            name="ck_max_memories_source",
        ),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1000", name="ck_max_memories_confidence"),
        sa.CheckConstraint(
            "state IN ('pending', 'approved', 'rejected')", name="ck_max_memories_state"
        ),
    )
    op.create_index(
        "ix_max_memories_lookup", "max_memories", ["owner_id", "scope", "state", "updated_at"]
    )
    op.create_table(
        "max_memory_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "memory_id",
            sa.String(36),
            sa.ForeignKey("max_memories.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_max_memory_events_owner", "max_memory_events", ["owner_id", "memory_id"]
    )


def downgrade() -> None:
    # Destructive: export/review owner memories before any downgrade.
    op.drop_index("ix_max_memory_events_owner", table_name="max_memory_events")
    op.drop_table("max_memory_events")
    op.drop_index("ix_max_memories_lookup", table_name="max_memories")
    op.drop_table("max_memories")
