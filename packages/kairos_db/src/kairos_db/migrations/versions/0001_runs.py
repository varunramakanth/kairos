"""Durable runs: runs, run_events, run_steps, tool_calls, tool_result_blobs.

Revision ID: 0001
Revises:
Create Date: 2026-11-16
"""

from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NOW = sa.text("now()")


def _ts(name: str, *, nullable: bool = False) -> sa.Column[datetime]:
    default = None if nullable else _NOW
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, server_default=default)


def _run_fk() -> sa.Column[str]:
    return sa.Column(
        "run_id", sa.Text, sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True
    )


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("thread_id", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("stop_reason", sa.Text, nullable=True),
        sa.Column("request", JSONB, nullable=False),
        sa.Column("state", JSONB, nullable=True),
        sa.Column("lease_id", sa.Text, nullable=True),
        _ts("lease_until", nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_runs_thread_id", "runs", ["thread_id"])
    op.create_index("ix_runs_due", "runs", ["status", "lease_until"])

    op.create_table(
        "run_events",
        _run_fk(),
        sa.Column("seq", sa.Integer, primary_key=True),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        _ts("created_at"),
    )

    op.create_table(
        "run_steps",
        _run_fk(),
        sa.Column("step", sa.Integer, primary_key=True),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("stop_reason", sa.Text, nullable=False),
        sa.Column("input_tokens", sa.Integer, nullable=False),
        sa.Column("output_tokens", sa.Integer, nullable=False),
        sa.Column("cache_read_input_tokens", sa.Integer, nullable=False),
        sa.Column("cache_creation_input_tokens", sa.Integer, nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=False),
        _ts("created_at"),
    )

    op.create_table(
        "tool_calls",
        _run_fk(),
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("step", sa.Integer, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("input", JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("output", sa.Text, nullable=True),
        sa.Column("is_error", sa.Boolean, nullable=True),
        sa.Column("duration_ms", sa.Integer, nullable=True),
        _ts("created_at"),
        _ts("finished_at", nullable=True),
    )

    op.create_table(
        "tool_result_blobs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("run_id", sa.Text, sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_tool_result_blobs_run_id", "tool_result_blobs", ["run_id"])


def downgrade() -> None:
    for table in ("tool_result_blobs", "tool_calls", "run_steps", "run_events", "runs"):
        op.drop_table(table)
