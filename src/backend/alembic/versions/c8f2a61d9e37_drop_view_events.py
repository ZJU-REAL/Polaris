"""移除浏览打点表 view_events（实验室热度榜的数据源，个人化定位下撤除）

同批移除：/lab/hot 热度榜、/lab/usage/leaderboard 用量排行榜及其管理员开关
（system_settings 里的 lab_leaderboard_enabled 键留存无害，不迁移清理）。

Revision ID: c8f2a61d9e37
Revises: b7d4f92e6c15
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8f2a61d9e37"
down_revision: str | None = "b7d4f92e6c15"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_view_events_target_created", table_name="view_events")
    op.drop_index("ix_view_events_kind_created", table_name="view_events")
    op.drop_table("view_events")


def downgrade() -> None:
    # 复原 a1c9e73b5d20 的形状（数据不可恢复）。
    op.create_table(
        "view_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_view_events_kind_created", "view_events", ["kind", "created_at"])
    op.create_index("ix_view_events_target_created", "view_events", ["target_id", "created_at"])
