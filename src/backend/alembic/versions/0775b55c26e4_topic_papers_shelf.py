"""课题「相关研究」书架（P5a）

新建 topic_papers：课题对内容池论文的引用 + 入架 wiki 快照 + 备注。
过渡期课题 = project，topic_id 外键 projects；source_library_id 可空
（个人补充入库为空；删库 SET NULL，书架行靠快照兜底）。

Revision ID: 0775b55c26e4
Revises: fe8a86942dc7
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0775b55c26e4"
down_revision: str | None = "fe8a86942dc7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "topic_papers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("source_library_id", sa.Uuid(), nullable=True),
        sa.Column("wiki_snapshot", sa.Text(), nullable=True),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("added_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["topic_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_library_id"], ["direction_libraries.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["added_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("topic_id", "paper_id", name="uq_topic_papers_topic_paper"),
    )
    op.create_index("ix_topic_papers_topic_id", "topic_papers", ["topic_id"])
    op.create_index("ix_topic_papers_paper_id", "topic_papers", ["paper_id"])


def downgrade() -> None:
    op.drop_index("ix_topic_papers_paper_id", table_name="topic_papers")
    op.drop_index("ix_topic_papers_topic_id", table_name="topic_papers")
    op.drop_table("topic_papers")
