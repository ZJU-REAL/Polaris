"""daily feed entries record which sources brought them

Revision ID: b5e2c8d41f7a
Revises: a8d3e5f71c42
Create Date: 2026-09-28

每日池以前不记条目来自哪个源，于是文献库的增量同步分不出来：一个只选了 PubMed 的库
会被送去对整池（多半是 arXiv 公告）做相似度排序（#821）。这一列记下带来这篇的源。

存量行按手上仅有的线索回填：有 arXiv 编号的是 arXiv 公告带来的；没有的只可能来自
另一个能供日更的源，也就是 PubMed（#778）。池子按保留期滚动，回填不准的影响也随之
在两周内消失。
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# 与同表 categories 列同一类型（f2d34705e152）：PG 上是 JSONB
_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")

revision = "b5e2c8d41f7a"
down_revision = "a8d3e5f71c42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("daily_feed_entries") as batch:
        batch.add_column(sa.Column("sources", _JSON, nullable=True))
    conn = op.get_bind()
    conn.execute(
        sa.text(
            "UPDATE daily_feed_entries SET sources = :arxiv WHERE paper_id IN "
            "(SELECT id FROM papers WHERE arxiv_id IS NOT NULL AND arxiv_id <> '')"
        ).bindparams(sa.bindparam("arxiv", ["arxiv"], type_=_JSON))
    )
    conn.execute(
        sa.text("UPDATE daily_feed_entries SET sources = :pubmed WHERE sources IS NULL").bindparams(
            sa.bindparam("pubmed", ["pubmed"], type_=_JSON)
        )
    )
    with op.batch_alter_table("daily_feed_entries") as batch:
        batch.alter_column("sources", existing_type=_JSON, nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("daily_feed_entries") as batch:
        batch.drop_column("sources")
