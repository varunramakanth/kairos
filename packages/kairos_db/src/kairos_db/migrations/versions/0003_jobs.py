"""Scheduled jobs and watches: jobs, job_runs, watch_observations, alerts.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("spec", JSONB, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("next_run_at", _TS, nullable=True),
        sa.Column("last_run_at", _TS, nullable=True),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("lease_id", sa.Text, nullable=True),
        sa.Column("lease_until", _TS, nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
    )
    op.create_index("ix_jobs_due", "jobs", ["status", "next_run_at"])

    op.create_table(
        "job_runs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("job_id", sa.Text, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column(
            "run_id",
            sa.Text,
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("scheduled_for", _TS, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("finished_at", _TS, nullable=True),
        # The last line of defence against double-firing: one run per scheduled slot.
        sa.UniqueConstraint("job_id", "scheduled_for", name="uq_job_runs_slot"),
    )
    op.create_index("ix_job_runs_status", "job_runs", ["status"])
    op.create_index("ix_job_runs_job_created", "job_runs", ["job_id", "created_at"])

    op.create_table(
        "watch_observations",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("job_id", sa.Text, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("observation", JSONB, nullable=False),
        sa.Column("matched", sa.Boolean, nullable=False),
        sa.Column("created_at", _TS, nullable=False),
    )
    op.create_index("ix_watch_observations_job", "watch_observations", ["job_id", "created_at"])

    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("job_id", sa.Text, sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("dedupe_key", sa.Text, nullable=False),
        sa.Column("created_at", _TS, nullable=False),
    )
    op.create_index("ix_alerts_job", "alerts", ["job_id", "created_at"])


def downgrade() -> None:
    for table in ("alerts", "watch_observations", "job_runs", "jobs"):
        op.drop_table(table)
