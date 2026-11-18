"""Eval runs and results, written by ``kairos_evals.PostgresSink``, read by the dashboard.

The columns match ``kairos_evals.sinks.SCHEMA_SQL``; an integration test writes
through the sink into this schema to keep the two in step.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("suite", sa.Text, nullable=False),
        sa.Column("harness", sa.Text, nullable=False),
        sa.Column("harness_version", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column("git_sha", sa.Text, nullable=True),
        sa.Column("started_at", _TS, nullable=False),
        sa.Column("finished_at", _TS, nullable=False),
        sa.Column("examples", sa.Integer, nullable=False),
        sa.Column("passed", sa.Boolean, nullable=True),
        sa.Column("summary", JSONB, nullable=False),
        sa.Column("gate", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_eval_runs_suite", "eval_runs", ["suite", "started_at"])
    op.create_table(
        "eval_results",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "run_id", sa.Text, sa.ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("example_id", sa.Text, nullable=True),
        sa.Column("trace_id", sa.Text, nullable=False),
        sa.Column("metric_id", sa.Text, nullable=False),
        sa.Column("layer", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("value", sa.Float, nullable=False),
        sa.Column("passed", sa.Boolean, nullable=False),
        sa.Column("applicable", sa.Boolean, nullable=False),
        sa.Column("measurement", sa.Float, nullable=True),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("details", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_eval_results_run", "eval_results", ["run_id", "metric_id"])


def downgrade() -> None:
    op.drop_table("eval_results")
    op.drop_table("eval_runs")
