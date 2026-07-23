"""库承载完整收录配置：direction_libraries.definition（P8a 配置权威化）

给方向库一个 ``definition`` JSON 列（结构与原 ``project.definition`` 一致：
statement/goals/in_scope/out_of_scope/questions/rubric/anchor_papers/keywords/
cadence），成为收录配置的唯一权威。数据迁移：每个隐式库（project_id 非空）从其
起源课题整体拷贝 definition；同时把 ingest_state（水位线/last_run）从起源课题拷回
库上——ingest 的水位线读写自此以库为准（见 actions_wiki.update_watermark）。

列到列拷贝（相关子查询 UPDATE）在 sqlite/postgres 均可用，且保留 JSON 原生表示，
无需 Python 端序列化。

Revision ID: b3e9c1f47a20
Revises: 67d18892a6ce
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3e9c1f47a20"
down_revision: str | None = "67d18892a6ce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    json_type = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
    op.add_column("direction_libraries", sa.Column("definition", json_type, nullable=True))

    # 隐式库从起源课题整体拷 definition + ingest_state（列到列，保留 JSON 原生表示）。
    bind.execute(
        sa.text(
            "UPDATE direction_libraries SET "
            "definition = (SELECT p.definition FROM projects p "
            "WHERE p.id = direction_libraries.project_id), "
            "ingest_state = (SELECT p.ingest_state FROM projects p "
            "WHERE p.id = direction_libraries.project_id) "
            "WHERE project_id IS NOT NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("direction_libraries") as batch:
        batch.drop_column("definition")
