"""Long-term memories, with Postgres full-text search.

A generated ``tsvector`` column and a GIN index give keyword recall now. Semantic
recall (a pgvector column) arrives with an embedding model in a later migration.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "memories",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("run_id", sa.Text, sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "search",
            TSVECTOR,
            sa.Computed("to_tsvector('english', content)", persisted=True),
            nullable=False,
        ),
    )
    op.create_index("ix_memories_search", "memories", ["search"], postgresql_using="gin")
    op.create_index("ix_memories_status", "memories", ["status", "updated_at"])


def downgrade() -> None:
    op.drop_table("memories")
