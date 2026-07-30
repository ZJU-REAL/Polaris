"""stamp library_papers with the run that scored them

后续步骤（取全文/编译/简报）以前按 ``scored_at >= run.created_at`` 认「本次收下的
那批论文」，墙钟回拨或 API/worker 时钟偏差会让刚打完分的论文落在窗口外被静默丢掉。
改成打分时记下运行 id，恒等比较，与时钟无关。

Revision ID: 78e222c38b3b
Revises: e6a1c9d4f207
Create Date: 2026-07-30 10:30:13.041541

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "78e222c38b3b"
down_revision: str | None = "e6a1c9d4f207"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("library_papers", sa.Column("scored_run_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_library_papers_scored_run_id", "library_papers", ["scored_run_id"], unique=False
    )
    # sqlite 不支持 ALTER TABLE ADD CONSTRAINT（要整表重建），存量 sqlite 库跳过外键
    if op.get_bind().dialect.name != "sqlite":
        op.create_foreign_key(
            "fk_library_papers_scored_run_id",
            "library_papers",
            "voyage_runs",
            ["scored_run_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_library_papers_scored_run_id", "library_papers", type_="foreignkey")
    op.drop_index("ix_library_papers_scored_run_id", table_name="library_papers")
    op.drop_column("library_papers", "scored_run_id")
