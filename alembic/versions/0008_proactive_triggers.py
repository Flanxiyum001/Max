"""Proactive trigger rules/runs and multi-step plan graph columns.

Revision ID: 0008_proactive_triggers
Revises: 0007_grant_proposal_uniqueness
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008_proactive_triggers"
down_revision: str | None = "0007_grant_proposal_uniqueness"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("max_tasks", sa.Column("parent_id", sa.String(36), nullable=True))
    op.create_index("ix_max_tasks_parent_id", "max_tasks", ["parent_id"])
    op.add_column("max_tasks", sa.Column("plan_position", sa.Integer(), nullable=True))
    op.create_table(
        "max_trigger_rules",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("schedule", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("last_fired_minute", sa.String(20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("owner_id", "kind", name="uq_trigger_owner_kind"),
    )
    op.create_index(
        "ix_trigger_rules_owner_enabled",
        "max_trigger_rules",
        ["owner_id", "enabled"],
    )
    op.create_table(
        "max_trigger_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("rule_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("output", sa.Text(), nullable=False),
        sa.Column("proposal_json", sa.Text(), nullable=True),
        sa.Column("approval_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_trigger_runs_rule_id", "max_trigger_runs", ["rule_id"])
    op.create_index(
        "ix_trigger_runs_owner_state",
        "max_trigger_runs",
        ["owner_id", "state", "created_at"],
    )


def downgrade() -> None:
    # Review outstanding runs before destructive rollback.
    op.drop_index("ix_trigger_runs_owner_state", table_name="max_trigger_runs")
    op.drop_index("ix_trigger_runs_rule_id", table_name="max_trigger_runs")
    op.drop_table("max_trigger_runs")
    op.drop_index("ix_trigger_rules_owner_enabled", table_name="max_trigger_rules")
    op.drop_table("max_trigger_rules")
    op.drop_index("ix_max_tasks_parent_id", table_name="max_tasks")
    op.drop_column("max_tasks", "plan_position")
    op.drop_column("max_tasks", "parent_id")
