"""引文边表 paper_citations（#639：引文意图分类 + OpenAlex 对齐）

每行一条「citing 论文 → 参考文献条目」边：条目原文永远保留（池外文献只有
这一份信息），cited_paper_id 是尽力而为的池内对齐（SET NULL 断链不删边），
intent/confidence 由 citation_intent 环节的 LLM 分类回填。

Revision ID: 57543f6328a1
Revises: 7e2b9f4c1a86
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "57543f6328a1"
down_revision: str | None = "7e2b9f4c1a86"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_citations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("citing_paper_id", sa.Uuid(), nullable=False),
        sa.Column("cited_paper_id", sa.Uuid(), nullable=True),
        sa.Column("ref_index", sa.Integer(), nullable=False),
        sa.Column("cited_ref_raw", sa.Text(), nullable=False),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("intent", sa.String(length=16), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["citing_paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cited_paper_id"], ["papers.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "citing_paper_id", "ref_index", name="uq_paper_citations_citing_ref"
        ),
    )
    # 查询习惯：详情页按 citing 取整篇引文，反查「谁引了这篇」按 cited
    op.create_index(
        "ix_paper_citations_citing_paper_id", "paper_citations", ["citing_paper_id"]
    )
    op.create_index(
        "ix_paper_citations_cited_paper_id", "paper_citations", ["cited_paper_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_paper_citations_cited_paper_id", table_name="paper_citations")
    op.drop_index("ix_paper_citations_citing_paper_id", table_name="paper_citations")
    op.drop_table("paper_citations")
