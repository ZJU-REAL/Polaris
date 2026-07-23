"""课题 × 文献库关联 + 库生命周期独立（P7 Step 1）

新建 topic_source_libraries（多对多关联行，回填每个课题关联自己的隐式库）；
direction_libraries.project_id 语义降级为「起源课题」溯源，ondelete 由 CASCADE
改 SET NULL——删课题不再级联删库（内容/成员行/概念保留，孤儿库由 admin 后续
处理）。原 FK/unique 均未命名（inline 建表），用 inspector 探活实际约束名再
drop，兼容 sqlite/postgres。

Revision ID: 67d18892a6ce
Revises: 3f770d85dca9
Create Date: 2026-07-23
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "67d18892a6ce"
down_revision: str | None = "3f770d85dca9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    op.create_table(
        "topic_source_libraries",
        sa.Column("topic_id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["topic_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["library_id"], ["direction_libraries.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("topic_id", "library_id"),
    )
    op.create_index(
        "ix_topic_source_libraries_library_id", "topic_source_libraries", ["library_id"]
    )

    # 回填：每个课题关联自己的隐式库（历史 1:1 数据）
    rows = bind.execute(
        sa.text(
            "SELECT project_id, id FROM direction_libraries WHERE project_id IS NOT NULL"
        )
    ).fetchall()
    if rows:
        table = sa.table(
            "topic_source_libraries",
            sa.column("topic_id", sa.Uuid()),
            sa.column("library_id", sa.Uuid()),
            sa.column("created_at", sa.DateTime(timezone=True)),
        )
        now = datetime.now(UTC)
        op.bulk_insert(
            table,
            [
                {"topic_id": project_id, "library_id": library_id, "created_at": now}
                for project_id, library_id in rows
            ],
        )

    # direction_libraries.project_id：FK 未命名（inline 建表），探活实际约束名再改
    # ondelete；unique 约束不动（一个课题至多起源一个库，语义不变）。
    inspector = sa.inspect(bind)
    fk_name = next(
        (
            fk["name"]
            for fk in inspector.get_foreign_keys("direction_libraries")
            if fk.get("constrained_columns") == ["project_id"]
        ),
        None,
    )
    with op.batch_alter_table("direction_libraries") as batch:
        if fk_name:
            batch.drop_constraint(fk_name, type_="foreignkey")
        batch.create_foreign_key(
            "fk_direction_libraries_project_id",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("direction_libraries") as batch:
        batch.drop_constraint("fk_direction_libraries_project_id", type_="foreignkey")
        batch.create_foreign_key(
            "fk_direction_libraries_project_id",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="CASCADE",
        )

    op.drop_index("ix_topic_source_libraries_library_id", table_name="topic_source_libraries")
    op.drop_table("topic_source_libraries")
