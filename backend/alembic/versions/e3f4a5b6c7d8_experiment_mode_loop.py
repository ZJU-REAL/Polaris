"""experiment mode loop: 实验任务归入动态循环档（docs/voyage-loop.md §3）

- voyage_runs：存量 kind=experiment 的 mode 回填为 loop（引擎只在驱动时对齐 mode，
  终态历史任务不再驱动，需迁移修正展示）

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-07-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: str | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE voyage_runs SET mode = 'loop' WHERE kind = 'experiment'")


def downgrade() -> None:
    op.execute("UPDATE voyage_runs SET mode = 'pipeline' WHERE kind = 'experiment'")
