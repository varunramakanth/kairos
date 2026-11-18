"""Threads: a conversation is a sequence of runs that share a thread.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "threads",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_threads_updated_at", "threads", ["updated_at"])
    op.create_foreign_key(
        "fk_runs_thread_id", "runs", "threads", ["thread_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_runs_thread_created", "runs", ["thread_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_runs_thread_created", table_name="runs")
    op.drop_constraint("fk_runs_thread_id", "runs", type_="foreignkey")
    op.drop_table("threads")
