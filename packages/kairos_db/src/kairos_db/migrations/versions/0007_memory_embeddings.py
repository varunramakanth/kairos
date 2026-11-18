"""Semantic memory recall: a pgvector column (384 dimensions, cosine) and an HNSW index.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-18
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("ALTER TABLE memories ADD COLUMN embedding vector(384)")
    op.execute("ALTER TABLE memories ADD COLUMN embedding_model text")
    op.execute(
        "CREATE INDEX ix_memories_embedding ON memories USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_memories_embedding")
    op.execute("ALTER TABLE memories DROP COLUMN embedding_model")
    op.execute("ALTER TABLE memories DROP COLUMN embedding")
