"""paper review m5c: manuscripts.review_passed per docs/api-m5-c.md

- manuscripts.review_passed BOOL NOT NULL DEFAULT false：评审通过标记
  （meta.rating ≥ 6 且无 fabricated 引用时置 true，submit 前置条件）
- revision_notes 内嵌在 manuscripts.fact_pack JSON 中，无需新列

Revision ID: f3b4c5d6e7a8
Revises: e1f2a3b4c5d6
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3b4c5d6e7a8"
down_revision: str | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("manuscripts") as batch:
        batch.add_column(
            sa.Column("review_passed", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("manuscripts") as batch:
        batch.drop_column("review_passed")
