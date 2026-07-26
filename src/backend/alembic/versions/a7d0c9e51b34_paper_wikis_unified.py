"""论文解读统一到 paper_wikis：每篇论文一份

建 paper_wikis（paper_id 唯一）并把存量解读合并进来，优先级：
library_papers.wiki_content（同一篇多份取 compiled_at 最新的）→ 没有的话取
daily_feed_entries.wiki_content。model 一并带上（compiled_model / wiki_model），
compiled_by 无从得知留空；created_at/updated_at 沿用原编译时间，编译徽标不失真。

**原列一律保留不删**（library_papers.wiki_content / compiled_at / compiled_model、
daily_feed_entries.wiki_content / wiki_model、user_library_entries.wiki_content、
topic_papers.wiki_snapshot）。这次只把读写切到新表，删列留到确认稳定之后——
迁移因此可回滚：downgrade 只删新表，存量解读原封不动还在老列里。

Revision ID: a7d0c9e51b34
Revises: b7f3d21c8a05
Create Date: 2026-07-25
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "a7d0c9e51b34"
down_revision: str | None = "b7f3d21c8a05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MISSING = object()  # 「这篇还没选中过任何解读」哨兵，与「选中了但时间为空」区分


def _is_newer(candidate: datetime | None, current: Any) -> bool:
    """候选编译时间是否比已选中的更新（时间为空视为最旧）。"""
    if current is _MISSING:
        return True
    if candidate is None:
        return False
    if current is None:
        return True
    return candidate > current


def _backfill() -> None:
    """存量解读合并成每篇一份（库版优先、同篇多份取最新；再退每日推送版）。"""
    conn = op.get_bind()
    meta = sa.MetaData()
    library_papers = sa.Table(
        "library_papers",
        meta,
        sa.Column("paper_id", sa.Uuid()),
        sa.Column("wiki_content", sa.Text()),
        sa.Column("compiled_model", sa.String(length=255)),
        sa.Column("compiled_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    daily_entries = sa.Table(
        "daily_feed_entries",
        meta,
        sa.Column("paper_id", sa.Uuid()),
        sa.Column("wiki_content", sa.Text()),
        sa.Column("wiki_model", sa.String(length=128)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    paper_wikis = sa.Table(
        "paper_wikis",
        meta,
        sa.Column("id", sa.Uuid()),
        sa.Column("paper_id", sa.Uuid()),
        sa.Column("content", sa.Text()),
        sa.Column("model", sa.String(length=128)),
        sa.Column("compiled_by", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )

    picked: dict[uuid.UUID, dict[str, Any]] = {}
    picked_at: dict[uuid.UUID, Any] = {}

    rows = conn.execute(
        sa.select(
            library_papers.c.paper_id,
            library_papers.c.wiki_content,
            library_papers.c.compiled_model,
            library_papers.c.compiled_at,
            library_papers.c.updated_at,
        ).where(library_papers.c.wiki_content.is_not(None))
    ).all()
    for paper_id, content, model, compiled_at, updated_at in rows:
        at = compiled_at or updated_at
        if not _is_newer(at, picked_at.get(paper_id, _MISSING)):
            continue
        picked_at[paper_id] = at
        picked[paper_id] = {
            "paper_id": paper_id,
            "content": content,
            "model": model[:128] if model else None,
            "at": at,
        }

    daily_rows = conn.execute(
        sa.select(
            daily_entries.c.paper_id,
            daily_entries.c.wiki_content,
            daily_entries.c.wiki_model,
            daily_entries.c.updated_at,
        ).where(daily_entries.c.wiki_content.is_not(None))
    ).all()
    for paper_id, content, model, updated_at in daily_rows:
        if paper_id in picked:
            continue  # 库版优先
        picked[paper_id] = {
            "paper_id": paper_id,
            "content": content,
            "model": model[:128] if model else None,
            "at": updated_at,
        }

    if not picked:
        return
    now = datetime.now(UTC)
    conn.execute(
        paper_wikis.insert(),
        [
            {
                "id": uuid.uuid4(),
                "paper_id": row["paper_id"],
                "content": row["content"],
                "model": row["model"],
                "compiled_by": None,
                # 时间列非空：两个来源时间都缺的极旧数据用当前时间兜底
                "created_at": row["at"] or now,
                "updated_at": row["at"] or now,
            }
            for row in picked.values()
        ],
    )


def upgrade() -> None:
    op.create_table(
        "paper_wikis",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("compiled_by", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["compiled_by"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("paper_id", name="uq_paper_wikis_paper"),
    )
    op.create_index("ix_paper_wikis_paper_id", "paper_wikis", ["paper_id"])
    _backfill()


def downgrade() -> None:
    # 存量解读仍在原列（本迁移只读不写它们），删表即回到迁移前状态
    op.drop_index("ix_paper_wikis_paper_id", table_name="paper_wikis")
    op.drop_table("paper_wikis")
