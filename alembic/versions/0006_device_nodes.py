"""Add revocable owner-scoped node public keys and one-use challenges.

Revision ID: 0006_device_nodes
Revises: 0005_memory_foundation
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0006_device_nodes"
down_revision: str | None = "0005_memory_foundation"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "max_device_nodes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("public_key_b64url", sa.String(512), nullable=False),
        sa.Column("key_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("owner_id", "key_sha256", name="uq_node_owner_key"),
        sa.CheckConstraint("state IN ('pending', 'active', 'revoked')", name="ck_node_state"),
    )
    op.create_index(
        "ix_nodes_owner_state", "max_device_nodes", ["owner_id", "state"]
    )
    op.create_table(
        "max_device_challenges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column(
            "device_id", sa.String(36),
            sa.ForeignKey("max_device_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("nonce_sha256", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "purpose IN ('enroll', 'authenticate')", name="ck_node_challenge_purpose"
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'consumed', 'revoked')", name="ck_node_challenge_state"
        ),
    )
    op.create_index(
        "ix_node_challenges_device", "max_device_challenges", ["owner_id", "device_id"]
    )


def downgrade() -> None:
    # Destructive: owner approval, backup and rollback plan required.
    op.drop_index("ix_node_challenges_device", table_name="max_device_challenges")
    op.drop_table("max_device_challenges")
    op.drop_index("ix_nodes_owner_state", table_name="max_device_nodes")
    op.drop_table("max_device_nodes")
