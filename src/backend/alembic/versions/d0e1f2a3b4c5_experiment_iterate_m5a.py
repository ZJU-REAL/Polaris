"""experiment iterate m5a: reflection/primary_value + figures/iteration_state per docs/api-m5-a.md §2

- experiment_runs.reflection JSON：该轮 LLM structured reflection 对象
- experiment_runs.primary_value FLOAT：主指标值（平台解析，direction 感知比较用）
- experiments.figures JSON：[{index, name, caption, path 内部}]
- experiments.iteration_state JSON：{no_improve_streak, debug_count, stopped_reason}

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c9d0e1f2a3b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# postgres 用 JSONB，sqlite 等回退通用 JSON（与 app.models.base.JSONVariant 一致）
JSONVariant = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column("experiment_runs", sa.Column("reflection", JSONVariant, nullable=True))
    op.add_column("experiment_runs", sa.Column("primary_value", sa.Float(), nullable=True))
    op.add_column("experiments", sa.Column("figures", JSONVariant, nullable=True))
    op.add_column("experiments", sa.Column("iteration_state", JSONVariant, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("experiments") as batch:
        batch.drop_column("iteration_state")
        batch.drop_column("figures")
    with op.batch_alter_table("experiment_runs") as batch:
        batch.drop_column("primary_value")
        batch.drop_column("reflection")
