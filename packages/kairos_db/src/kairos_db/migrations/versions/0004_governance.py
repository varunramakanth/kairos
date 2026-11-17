"""Approvals, audit log, notification outbox, push subscriptions, app settings.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("run_id", sa.Text, sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("tool_call_id", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("input", JSONB, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("risk", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", _TS, nullable=True),
        sa.UniqueConstraint("run_id", "tool_call_id", name="uq_approvals_call"),
    )
    op.create_index("ix_approvals_status", "approvals", ["status", "created_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("actor", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("subject", sa.Text, nullable=False),
        sa.Column("run_id", sa.Text, nullable=True),
        sa.Column("detail", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])

    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("results", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("dedupe_key", sa.Text, nullable=True, unique=True),
        sa.Column("next_attempt_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("lease_until", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", _TS, nullable=True),
    )
    op.create_index("ix_notifications_due", "notifications", ["status", "next_attempt_at"])

    op.create_table(
        "push_subscriptions",
        sa.Column("endpoint", sa.Text, primary_key=True),
        sa.Column("keys", JSONB, nullable=False),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text, primary_key=True),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.func.now()),
    )

    # A digest firing has no agent run.
    op.alter_column("job_runs", "run_id", nullable=True)


def downgrade() -> None:
    op.alter_column("job_runs", "run_id", nullable=False)
    for table in ("app_settings", "push_subscriptions", "notifications", "audit_log", "approvals"):
        op.drop_table(table)
