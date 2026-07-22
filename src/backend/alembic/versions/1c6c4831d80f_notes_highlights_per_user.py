"""笔记 / 划线归属拆分（P5b）

PaperNote / PaperHighlight 作用域从 project × paper 改为 paper × author：
一个人在一篇论文上的划线与批注跨课题共享。删 project_id 列即可——
原表按 project 隔离的行本就各归其作者，删列后天然合并，不丢行。

Revision ID: 1c6c4831d80f
Revises: 0775b55c26e4
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "1c6c4831d80f"
down_revision: str | None = "0775b55c26e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("paper_notes", "paper_highlights"):
        with op.batch_alter_table(table) as batch:
            batch.drop_index(f"ix_{table}_project_id")
            batch.drop_column("project_id")


def downgrade() -> None:
    # 有损降级：project_id 数据已丢弃，只能补回可空列（原列 NOT NULL）
    for table in ("paper_notes", "paper_highlights"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("project_id", sa.Uuid(), nullable=True))
            batch.create_index(f"ix_{table}_project_id", ["project_id"])
