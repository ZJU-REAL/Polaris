"""manuscript 置顶 pinned_at

- manuscripts.pinned_at：非空即置顶，列表排在前面

Revision ID: 5b1f3a9c72e6
Revises: 3c7e9a1b8d24
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "5b1f3a9c72e6"
down_revision: str | None = "3c7e9a1b8d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("manuscripts", sa.Column("pinned_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("manuscripts", "pinned_at")
