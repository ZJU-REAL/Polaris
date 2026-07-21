"""voyage terminal logs (persisted task terminal history)

Revision ID: d800d34cd207
Revises: 877da11ed6b4
Create Date: 2026-07-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d800d34cd207"
down_revision: str | None = "877da11ed6b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voyage_terminal_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("event", sa.String(length=16), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=True),
        sa.Column("stage", sa.String(length=32), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["voyage_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_voyage_terminal_logs_run_id"),
        "voyage_terminal_logs",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_voyage_terminal_logs_run_id"), table_name="voyage_terminal_logs")
    op.drop_table("voyage_terminal_logs")
