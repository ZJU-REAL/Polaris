"""manuscript 垃圾箱：软删除 trashed_at

- manuscripts.trashed_at：非空即在垃圾箱，列表默认过滤；清空垃圾箱才真正删除

Revision ID: 3c7e9a1b8d24
Revises: 26dfa5fd661e
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3c7e9a1b8d24"
down_revision: str | None = "26dfa5fd661e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("manuscripts", sa.Column("trashed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("manuscripts", "trashed_at")
