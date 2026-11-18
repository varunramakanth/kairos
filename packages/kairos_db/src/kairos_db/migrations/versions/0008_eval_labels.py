"""Human labels on judge verdicts, for judge calibration (review queue).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_labels",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "run_id", sa.Text, sa.ForeignKey("eval_runs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("example_id", sa.Text, nullable=False),
        sa.Column("metric_id", sa.Text, nullable=False),
        sa.Column("human_passed", sa.Boolean, nullable=False),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "labeled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("run_id", "example_id", "metric_id", name="uq_eval_labels_item"),
    )


def downgrade() -> None:
    op.drop_table("eval_labels")
