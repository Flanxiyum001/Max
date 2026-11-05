"""Approval grants, trust audit and independent verification records.

Revision ID: 0002_trust_verification
Revises: 0001_durable_tasks
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0002_trust_verification"
down_revision: str | None = "0001_durable_tasks"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "max_approval_grants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("max_tasks.id"), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("binding_digest", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_max_approval_owner_state", "max_approval_grants", ["owner_id", "state"]
    )
    op.create_table(
        "max_trust_audit",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("max_tasks.id"), nullable=False),
        sa.Column("approval_id", sa.String(36), nullable=True),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("reason", sa.String(60), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_max_trust_audit_task", "max_trust_audit", ["task_id", "created_at"])
    op.create_table(
        "max_verification_checks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("max_tasks.id"), nullable=False),
        sa.Column("observed_digest", sa.String(64), nullable=True),
        sa.Column("expected_digest", sa.String(64), nullable=False),
        sa.Column("verdict", sa.String(30), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_max_verification_task", "max_verification_checks", ["task_id", "checked_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_max_verification_task", table_name="max_verification_checks")
    op.drop_table("max_verification_checks")
    op.drop_index("ix_max_trust_audit_task", table_name="max_trust_audit")
    op.drop_table("max_trust_audit")
    op.drop_index("ix_max_approval_owner_state", table_name="max_approval_grants")
    op.drop_table("max_approval_grants")
