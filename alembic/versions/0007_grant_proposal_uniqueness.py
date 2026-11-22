"""Keep the owner approval list clean: one pending grant per proposal.

Adds a partial unique index on pending (owner_id, task_id, proposal_id) and a
covering index so list_pending stays cheap as the grants table grows.

Revision ID: 0007_grant_proposal_uniqueness
Revises: 0006_device_nodes
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text

revision: str = "0007_grant_proposal_uniqueness"
down_revision: str | None = "0006_device_nodes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        text(
            "DELETE FROM max_approval_grants WHERE state = 'pending' AND id NOT IN ("
            "SELECT id FROM ("
            "SELECT id, ROW_NUMBER() OVER ("
            "PARTITION BY owner_id, task_id, proposal_id ORDER BY created_at DESC, id DESC"
            ") AS rn FROM max_approval_grants WHERE state = 'pending'"
            ") ranked WHERE rn = 1)"
        )
    )
    op.drop_index("ix_max_approval_owner_state", table_name="max_approval_grants")
    op.create_index(
        "ix_max_approval_owner_state_expiry",
        "max_approval_grants",
        ["owner_id", "state", "expires_at"],
    )
    op.create_index(
        "uq_grant_owner_task_pending_proposal",
        "max_approval_grants",
        ["owner_id", "task_id", "proposal_id"],
        unique=True,
        sqlite_where=text("state = 'pending'"),
        postgresql_where=text("state = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_grant_owner_task_pending_proposal", table_name="max_approval_grants"
    )
    op.drop_index("ix_max_approval_owner_state_expiry", table_name="max_approval_grants")
    op.create_index(
        "ix_max_approval_owner_state", "max_approval_grants", ["owner_id", "state"]
    )
