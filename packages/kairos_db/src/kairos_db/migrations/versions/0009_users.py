"""Users and ownership (ADR-018).

Creates ``users`` with the ``local`` admin, gives every existing row to it, and adds a
``user_id`` to each root table. The column has no default once the backfill is done,
so an insert that forgets the owner fails instead of landing on the wrong user.

Foreign keys are ``ON UPDATE CASCADE``: binding the owner's first Cognito sign-in
renames ``local`` to their ``sub`` in one statement and their data follows.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-21
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TS = sa.DateTime(timezone=True)

OWNED = (
    "threads",
    "runs",
    "jobs",
    "memories",
    "audit_log",
    "notifications",
    "push_subscriptions",
)
"""Root tables. Child rows (events, steps, approvals, job runs...) belong to a parent."""

_INDEXED = {
    "threads": "updated_at",
    "runs": "created_at",
    "jobs": "created_at",
    "memories": "updated_at",
    "audit_log": "id",
    "notifications": "id",
}


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("email", sa.Text, nullable=True),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("daily_cap_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column("email_verified", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("disabled", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_at", _TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_users_role"),
    )
    op.create_index("ux_users_email", "users", [sa.text("lower(email)")], unique=True)
    op.execute(
        "INSERT INTO users (id, email, role, email_verified) VALUES ('local', NULL, 'admin', true)"
    )

    for table in OWNED:
        op.add_column(table, sa.Column("user_id", sa.Text, nullable=False, server_default="local"))
        op.alter_column(table, "user_id", server_default=None)
        op.create_foreign_key(
            f"fk_{table}_user",
            table,
            "users",
            ["user_id"],
            ["id"],
            onupdate="CASCADE",
            ondelete="CASCADE",
        )
        if column := _INDEXED.get(table):
            op.create_index(f"ix_{table}_user", table, ["user_id", column])


def downgrade() -> None:
    for table in OWNED:
        if table in _INDEXED:
            op.drop_index(f"ix_{table}_user", table_name=table)
        op.drop_constraint(f"fk_{table}_user", table, type_="foreignkey")
        op.drop_column(table, "user_id")
    op.drop_index("ux_users_email", table_name="users")
    op.drop_table("users")
