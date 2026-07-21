"""paper trash reason: 垃圾桶原因标签（不相关 / 手动删除）

- papers：+trash_reason（irrelevant 相关性不足自动淘汰 | manual 手动删除；仅 excluded 有值）
- 回填：存量 excluded 且打过分 → irrelevant，其余 excluded → manual

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("trash_reason", sa.String(length=16), nullable=True))
    op.execute(
        "UPDATE papers SET trash_reason = 'irrelevant' "
        "WHERE status = 'excluded' AND relevance_score IS NOT NULL"
    )
    op.execute(
        "UPDATE papers SET trash_reason = 'manual' "
        "WHERE status = 'excluded' AND relevance_score IS NULL"
    )


def downgrade() -> None:
    op.drop_column("papers", "trash_reason")
