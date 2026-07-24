"""文献库个人/公共归属：direction_libraries.is_public（P10）

- direction_libraries 新增 is_public（Boolean，NOT NULL，server_default=false）：
  个人库 false（仅创建者 + admin 可见/可管理）| 公共库 true（全实验室可见）。
- 回填：存量 status='active' 的库本来就全员可读，迁移为公共库
  （UPDATE ... SET is_public=true WHERE status='active'），保留现有可见性不回归。

Revision ID: 47b973fb7e13
Revises: 3ecc41527559
Create Date: 2026-07-24
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "47b973fb7e13"
down_revision: str | None = "3ecc41527559"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("direction_libraries") as batch:
        batch.add_column(
            sa.Column(
                "is_public", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
    # 回填：存量 active 库本来就全员可读 → 迁为公共库（保留现有可见性）。
    op.execute(
        sa.text(
            "UPDATE direction_libraries SET is_public = true WHERE status = 'active'"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("direction_libraries") as batch:
        batch.drop_column("is_public")
