"""voyage loop v1: 任务循环地基（docs/voyage-loop.md §8 阶段 A）

- voyage_runs：+mode（pipeline|template|loop，按 kind 回填）、+plan_iteration、+done_criteria
- voyage_steps：+rank（清单序 = 执行序，gap 编号回填 seq*100；seq 冻结为创建序）、
  +acceptance（结构化验收）、+requires_gate、+budget、+attempt、+attempts（尝试归档）、
  +provenance（计划迭代溯源）

Revision ID: c1d2e3f4a5b6
Revises: b8c9d0e1f2a3
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# kind → mode 回填（docs/voyage-loop.md §3；未知 kind = LLM 自由规划 → loop）
_PIPELINE_KINDS = (
    "wiki_bootstrap",
    "wiki_ingest",
    "idea_forge",
    "idea_review",
    "experiment",
    "paper_writing",
    "paper_review",
    "presentation",
)


def upgrade() -> None:
    op.add_column(
        "voyage_runs",
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="loop"),
    )
    op.add_column(
        "voyage_runs",
        sa.Column("plan_iteration", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("voyage_runs", sa.Column("done_criteria", sa.JSON(), nullable=True))

    op.add_column(
        "voyage_steps",
        sa.Column("rank", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column("voyage_steps", sa.Column("acceptance", sa.JSON(), nullable=True))
    op.add_column(
        "voyage_steps", sa.Column("requires_gate", sa.String(length=64), nullable=True)
    )
    op.add_column("voyage_steps", sa.Column("budget", sa.JSON(), nullable=True))
    op.add_column(
        "voyage_steps",
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("voyage_steps", sa.Column("attempts", sa.JSON(), nullable=True))
    op.add_column("voyage_steps", sa.Column("provenance", sa.JSON(), nullable=True))

    # 回填：mode 按 kind 映射；rank = seq * 100（存量清单序 = 创建序）
    kinds = ", ".join(f"'{k}'" for k in _PIPELINE_KINDS)
    op.execute(
        f"UPDATE voyage_runs SET mode = 'pipeline' WHERE kind IN ({kinds})"  # noqa: S608
    )
    op.execute("UPDATE voyage_runs SET mode = 'template' WHERE kind = 'idea_proposal'")
    op.execute("UPDATE voyage_steps SET rank = seq * 100")


def downgrade() -> None:
    op.drop_column("voyage_steps", "provenance")
    op.drop_column("voyage_steps", "attempts")
    op.drop_column("voyage_steps", "attempt")
    op.drop_column("voyage_steps", "budget")
    op.drop_column("voyage_steps", "requires_gate")
    op.drop_column("voyage_steps", "acceptance")
    op.drop_column("voyage_steps", "rank")
    op.drop_column("voyage_runs", "done_criteria")
    op.drop_column("voyage_runs", "plan_iteration")
    op.drop_column("voyage_runs", "mode")
