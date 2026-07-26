"""概念转正门槛：concepts.status / validated_at + 按现有关联数重判存量

概念此前是「模型在解读里标什么就建什么」，没有任何把关：89% 的词条只被 1 篇论文引用，
长尾几乎全是那篇论文自己起的名字（benchmark 名、数据集名、模型代号），把概念库淹了。

本迁移加两列：
- ``concepts.status``：``candidate``（默认，还只有 1 篇论文用到，不可见、不生成定义）
  / ``active``（转正，进概念库）/ ``rejected``（模型判定根本不是概念，终态）；
- ``concepts.validated_at``：最近一次被模型判定「确实是个学术概念」的时间。

存量按**现有关联数**重判：``paper_concepts`` 里关联到 ≥2 篇论文的置 active，其余留
candidate。``validated_at`` 一律留空——这批老词条从没被判过有效性，``fig:1``（生产库里
被 35 篇论文各标一次）这类靠关联数根本拦不住，得由模型判。判定要调 LLM，而迁移必须能
离线、确定性、随容器启动跑完，所以不在这里调：留空的 ``validated_at`` 就是待判标记，
下一次概念同步（每日任务的上链步骤，或概念库页面上手点一次「从已编译论文提取概念」）
会连同定义一起复核一遍，无效的自动下架（services/concepts.py::review_active_concepts）。

**一行都不删**：降级为候选的概念连同它们已经花过钱的定义原样保留，将来被第二篇论文
引用时直接转正复用。downgrade 把全部概念置回 active（= 迁移前「全都可见」的状态）
再删列。

Revision ID: a9f1c62b70d5
Revises: c4e7b2a91f38
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9f1c62b70d5"
down_revision: str | None = "c4e7b2a91f38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 转正门槛（与 services/concepts.py::CONCEPT_PROMOTION_MIN_PAPERS 一致）
MIN_PAPERS = 2
_UPDATE_CHUNK = 500


def _concepts_table() -> tuple[sa.Table, sa.Table]:
    meta = sa.MetaData()
    concepts = sa.Table(
        "concepts",
        meta,
        sa.Column("id", sa.Uuid()),
        sa.Column("name", sa.String(length=255)),
        sa.Column("status", sa.String(length=16)),
    )
    paper_concepts = sa.Table(
        "paper_concepts",
        meta,
        sa.Column("paper_id", sa.Uuid()),
        sa.Column("concept_id", sa.Uuid()),
    )
    return concepts, paper_concepts


def upgrade() -> None:
    op.add_column(
        "concepts",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="candidate"),
    )
    op.add_column(
        "concepts", sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True)
    )
    conn = op.get_bind()
    concepts, paper_concepts = _concepts_table()
    # 关联数 ≥ 门槛的概念（引用计数不分论文 status，与运行时的转正/孤儿判定同口径）
    promote = list(
        conn.execute(
            sa.select(paper_concepts.c.concept_id)
            .group_by(paper_concepts.c.concept_id)
            .having(sa.func.count() >= MIN_PAPERS)
        ).scalars()
    )
    for i in range(0, len(promote), _UPDATE_CHUNK):
        conn.execute(
            sa.update(concepts)
            .where(concepts.c.id.in_(promote[i : i + _UPDATE_CHUNK]))
            .values(status="active")
        )


def downgrade() -> None:
    # 回到迁移前的可见状态：全部 active。列随后就删掉，这句是为了「先还原语义再删列」
    # 的顺序清楚——万一列被别处保留下来，语义也是对的。
    concepts, _ = _concepts_table()
    op.get_bind().execute(sa.update(concepts).values(status="active"))
    op.drop_column("concepts", "validated_at")
    op.drop_column("concepts", "status")
