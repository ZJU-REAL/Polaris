"""buddy memory kind

Revision ID: d4e8b19c7a55
Revises: 07e7faea4c7a
Create Date: 2026-08-05

记忆分两层：fact（每轮带上的稳定事实）与 note（带时间戳的片段，检索到才回上下文）。
存量行按 fact 处理——它们本来就是用户手写的、希望一直生效的那种。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "d4e8b19c7a55"
down_revision: str | None = "07e7faea4c7a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "buddy_memories",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="fact"),
    )


def downgrade() -> None:
    op.drop_column("buddy_memories", "kind")
