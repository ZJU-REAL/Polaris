"""paper highlights: PDF 划线标注表（阅读器）

paper_highlights：论文划线（paper/project/author 外键、page 页码、
rects 归一化矩形 JSON、selected_text 选中原文、color 颜色、note 可选批注）。
权限同 paper_notes。

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f4a5b6c7d8e9"
down_revision: str | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# postgres 用 JSONB，sqlite 等回退通用 JSON（与 app.models.base.JSONVariant 一致）
JSONVariant = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "paper_highlights",
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("rects", JSONVariant, nullable=False),
        sa.Column("selected_text", sa.Text(), nullable=False),
        sa.Column("color", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_paper_highlights_paper_id"), "paper_highlights", ["paper_id"], unique=False
    )
    op.create_index(
        op.f("ix_paper_highlights_project_id"), "paper_highlights", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_paper_highlights_author_id"), "paper_highlights", ["author_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_paper_highlights_author_id"), table_name="paper_highlights")
    op.drop_index(op.f("ix_paper_highlights_project_id"), table_name="paper_highlights")
    op.drop_index(op.f("ix_paper_highlights_paper_id"), table_name="paper_highlights")
    op.drop_table("paper_highlights")
