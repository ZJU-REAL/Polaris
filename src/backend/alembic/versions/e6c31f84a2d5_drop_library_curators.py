"""移除文献库策展人表（实验室共管机制，个人化定位下撤除）

一并移除 _pr3_backfilled_curators（b3d81f6c05a9 的回滚簿记表——主表没了它就
没有意义）。同批移除：库转公共审批流（request-public/approve/reject/
cancel/make-personal 端点）。is_public/status/review_note 列保留
（pending/rejected 成为不可达状态，列随后续 role/成员移除一并退役）。

Revision ID: e6c31f84a2d5
Revises: d4b8e26f1a93
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e6c31f84a2d5"
down_revision: str | None = "d4b8e26f1a93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("direction_library_curators")
    op.drop_table("_pr3_backfilled_curators")


def downgrade() -> None:
    # 复原 f7c2abfe8aeb 与 b3d81f6c05a9 的形状（数据不可恢复）。
    op.create_table(
        "direction_library_curators",
        sa.Column(
            "library_id",
            sa.Uuid(),
            sa.ForeignKey("direction_libraries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "_pr3_backfilled_curators",
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("library_id", "user_id"),
    )
