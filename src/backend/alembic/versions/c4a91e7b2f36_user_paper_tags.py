"""个人标签表 user_paper_tags

每个用户给论文打自己的标签，只有本人可见可改。与库作用域的 paper_tags 完全
独立：库标签是共享资产（整组覆盖会盖掉同库其他人打的），个人标签是私域。

扁平结构（name 内联，不建独立标签实体）：个人标签没有跨用户共享语义，不需要
「改一处到处生效」，也就不需要零引用回收。双 CASCADE 与 paper_user_meta /
paper_notes 一致——删用户或删论文时这些行随之清理。

Revision ID: c4a91e7b2f36
Revises: b3d81f6c05a9
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4a91e7b2f36"
down_revision: str | None = "c5e2a90d41f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_paper_tags",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "paper_id", "name", name="uq_user_paper_tags"),
    )
    op.create_index("ix_user_paper_tags_user_id", "user_paper_tags", ["user_id"])
    op.create_index("ix_user_paper_tags_paper_id", "user_paper_tags", ["paper_id"])
    # 「我的所有标签」列表与按标签筛选的取行路径
    op.create_index("ix_user_paper_tags_user_name", "user_paper_tags", ["user_id", "name"])


def downgrade() -> None:
    op.drop_index("ix_user_paper_tags_user_name", table_name="user_paper_tags")
    op.drop_index("ix_user_paper_tags_paper_id", table_name="user_paper_tags")
    op.drop_index("ix_user_paper_tags_user_id", table_name="user_paper_tags")
    op.drop_table("user_paper_tags")
