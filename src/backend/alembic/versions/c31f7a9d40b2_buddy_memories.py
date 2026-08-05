"""buddy memories

Revision ID: c31f7a9d40b2
Revises: a22aa895244c
Create Date: 2026-08-05

用户自己写给 Buddy 的长期记忆。本期**只有用户能写**：agent 自己往里写属于写工具那
一期，与权限模型一起做。分表而不是塞进 users.settings，是因为它要按条增删、要有
创建时间、将来还要有来源（谁写的、哪场对话里学到的）。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "c31f7a9d40b2"
down_revision: str | None = "a22aa895244c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "buddy_memories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("buddy_memories")
