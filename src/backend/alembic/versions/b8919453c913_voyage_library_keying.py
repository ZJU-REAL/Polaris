"""任务系统库化：voyage_runs / activities 可挂方向库（P9a）

- voyage_runs.project_id 改可空，新增 library_id（FK→direction_libraries，SET NULL，index）：
  ingest 类任务可直接挂库运行，管理员创建的独立库（project_id=NULL）也能触发抓取；
  起源课题的隐式库两者都带（兼容活动流/鉴权）。
- activities.project_id 改可空，新增 library_id（SET NULL，index）：独立库的 ingest
  活动记到库上（project_id 为空），课题活动照旧。

Revision ID: b8919453c913
Revises: b3e9c1f47a20
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8919453c913"
down_revision: str | None = "b3e9c1f47a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("voyage_runs") as batch:
        batch.alter_column(
            "project_id", existing_type=sa.Uuid(), nullable=True, existing_nullable=False
        )
        batch.add_column(sa.Column("library_id", sa.Uuid(), nullable=True))
        batch.create_index("ix_voyage_runs_library_id", ["library_id"])
        batch.create_foreign_key(
            "fk_voyage_runs_library_id",
            "direction_libraries",
            ["library_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("activities") as batch:
        batch.alter_column(
            "project_id", existing_type=sa.Uuid(), nullable=True, existing_nullable=False
        )
        batch.add_column(sa.Column("library_id", sa.Uuid(), nullable=True))
        batch.create_index("ix_activities_library_id", ["library_id"])
        batch.create_foreign_key(
            "fk_activities_library_id",
            "direction_libraries",
            ["library_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    # 回退前把库级（无课题）行清掉：project_id 要收回 NOT NULL，独立库任务/活动无处安放。
    op.execute("DELETE FROM activities WHERE project_id IS NULL")
    op.execute("DELETE FROM voyage_runs WHERE project_id IS NULL")
    with op.batch_alter_table("activities") as batch:
        batch.drop_constraint("fk_activities_library_id", type_="foreignkey")
        batch.drop_index("ix_activities_library_id")
        batch.drop_column("library_id")
        batch.alter_column(
            "project_id", existing_type=sa.Uuid(), nullable=False, existing_nullable=True
        )
    with op.batch_alter_table("voyage_runs") as batch:
        batch.drop_constraint("fk_voyage_runs_library_id", type_="foreignkey")
        batch.drop_index("ix_voyage_runs_library_id")
        batch.drop_column("library_id")
        batch.alter_column(
            "project_id", existing_type=sa.Uuid(), nullable=False, existing_nullable=True
        )
