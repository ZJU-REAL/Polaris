"""假设/实验树节点表 hypothesis_nodes（#637，设计报告 §8.2）

discovery run 的一等实体：Navigator 在树上分支/剪枝，恢复即从最优 open 节点
继续。本迁移只建表；树的读 API 与服务层同 PR 落地，引擎写入是 D2/D3 的事。

Revision ID: 7e2b9f4c1a86
Revises: b0dd1709e2a1
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "7e2b9f4c1a86"
down_revision: str | None = "b0dd1709e2a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "hypothesis_nodes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        # 自引用：NULL = 树根（每 run 单根由服务层保证）；父删子随删
        sa.Column("parent_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("grounding", _JSON, nullable=True),
        sa.Column("novelty_report", _JSON, nullable=True),
        sa.Column("feasibility", _JSON, nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["voyage_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["parent_id"], ["hypothesis_nodes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_hypothesis_nodes_run_id"), "hypothesis_nodes", ["run_id"], unique=False)
    # best_open_node / 树读取都按 run 过滤后再看 status，status 单列索引足够本期热查询
    op.create_index(op.f("ix_hypothesis_nodes_status"), "hypothesis_nodes", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_hypothesis_nodes_status"), table_name="hypothesis_nodes")
    op.drop_index(op.f("ix_hypothesis_nodes_run_id"), table_name="hypothesis_nodes")
    op.drop_table("hypothesis_nodes")
