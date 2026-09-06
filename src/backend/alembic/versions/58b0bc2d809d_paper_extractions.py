"""结构化抽取产物表 paper_extractions（#661：schema 引导抽取运行时）

每行一份「某 schema 对某论文」的抽取结果：payload 是按 schema 字段白名单
归一化后的 JSON（通用骨架 skeleton@1 = problem/method/findings/limitations），
confidence 是模型自评置信度（可空），stage_meta 记 {model, stage, version} 溯源。
同一 (paper_id, schema_id) 唯一——重抽是 UPSERT 覆盖，不留历史版本。

Revision ID: 58b0bc2d809d
Revises: 57543f6328a1
Create Date: 2026-09-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "58b0bc2d809d"
down_revision: str | None = "57543f6328a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 与 models/base.py 的 JSONVariant 同口径：postgres 用 JSONB，sqlite 回退通用 JSON
_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "paper_extractions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("schema_id", sa.String(length=64), nullable=False),
        sa.Column("payload", _JSON, nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("stage_meta", _JSON, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "paper_id", "schema_id", name="uq_paper_extractions_paper_schema"
        ),
    )
    # 查询习惯：详情页按论文取全部 schema 的产物
    op.create_index("ix_paper_extractions_paper_id", "paper_extractions", ["paper_id"])


def downgrade() -> None:
    op.drop_index("ix_paper_extractions_paper_id", table_name="paper_extractions")
    op.drop_table("paper_extractions")
