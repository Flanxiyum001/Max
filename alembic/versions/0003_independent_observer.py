"""Add attempt-bound deterministic observer provenance without rewriting legacy checks.

Revision ID: 0003_independent_observer
Revises: 0002_trust_verification
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0003_independent_observer"
down_revision: str | None = "0002_trust_verification"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Historical verification rows retain NULL on every new field.
    for name, kind in (
        ("request_id", sa.String(100)),
        ("request_fingerprint", sa.String(64)),
        ("task_revision", sa.Integer()),
        ("checkpoint_id", sa.String(36)),
        ("input_digest", sa.String(64)),
        ("operation_id", sa.String(100)),
        ("observer_id", sa.String(64)),
        ("observer_version", sa.String(64)),
        ("source_kind", sa.String(64)),
        ("reason_code", sa.String(64)),
        ("result_state", sa.String(32)),
        ("result_revision", sa.Integer()),
    ):
        op.add_column("max_verification_checks", sa.Column(name, kind, nullable=True))
    op.create_unique_constraint(
        "uq_observer_owner_task_request",
        "max_verification_checks",
        ["owner_id", "task_id", "request_id"],
    )
    op.create_unique_constraint(
        "uq_observer_owner_request",
        "max_verification_checks",
        ["owner_id", "request_id"],
    )


def downgrade() -> None:
    # Review and preserve verification provenance before running a destructive downgrade.
    op.drop_constraint("uq_observer_owner_request", "max_verification_checks", type_="unique")
    op.drop_constraint("uq_observer_owner_task_request", "max_verification_checks", type_="unique")
    for name in (
        "result_revision",
        "result_state",
        "reason_code",
        "source_kind",
        "observer_version",
        "observer_id",
        "operation_id",
        "input_digest",
        "checkpoint_id",
        "task_revision",
        "request_fingerprint",
        "request_id",
    ):
        op.drop_column("max_verification_checks", name)
