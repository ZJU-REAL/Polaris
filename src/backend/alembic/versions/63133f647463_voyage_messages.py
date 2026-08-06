"""voyage messages (per-run conversation stream between user and task agent)

Revision ID: 63133f647463
Revises: b3f5c1e07a92
Create Date: 2026-08-06 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "63133f647463"
down_revision: str | None = "b3f5c1e07a92"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "voyage_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reply_to", sa.Uuid(), nullable=True),
        sa.Column("step_id", sa.Uuid(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_step_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["voyage_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reply_to"], ["voyage_messages.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["step_id"], ["voyage_steps.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["consumed_step_id"], ["voyage_steps.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "seq", name="uq_voyage_messages_run_seq"),
    )
    op.create_index(op.f("ix_voyage_messages_run_id"), "voyage_messages", ["run_id"], unique=False)
    op.create_index(
        "ix_voyage_messages_run_kind_status",
        "voyage_messages",
        ["run_id", "kind", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_voyage_messages_run_kind_status", table_name="voyage_messages")
    op.drop_index(op.f("ix_voyage_messages_run_id"), table_name="voyage_messages")
    op.drop_table("voyage_messages")
