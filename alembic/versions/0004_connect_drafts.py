"""Durable, review-only MAX Connect event drafts.

Revision ID: 0004_connect_drafts
Revises: 0003_independent_observer
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004_connect_drafts"
down_revision: str | None = "0003_independent_observer"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "max_connect_drafts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("event_key", sa.String(100), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("resource_key", sa.String(80), nullable=False),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("draft", sa.String(500), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("owner_id", "source", "event_key", name="uq_connect_source_event"),
    )
    op.create_index(
        "ix_connect_owner_state_created",
        "max_connect_drafts",
        ["owner_id", "state", "created_at"],
    )


def downgrade() -> None:
    # Review and export outstanding drafts before destructive rollback.
    op.drop_index("ix_connect_owner_state_created", table_name="max_connect_drafts")
    op.drop_table("max_connect_drafts")
