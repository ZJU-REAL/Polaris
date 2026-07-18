"""paper highlights: 标注样式列 style（highlight / underline / wave）

Revision ID: c7d8e9f0a1b2
Revises: e3f4a5b6c7d8
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "paper_highlights",
        sa.Column("style", sa.String(length=16), nullable=False, server_default="highlight"),
    )


def downgrade() -> None:
    with op.batch_alter_table("paper_highlights") as batch:
        batch.drop_column("style")
