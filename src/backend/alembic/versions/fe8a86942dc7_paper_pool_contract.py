"""论文内容池收尾（P4 迁移 B）

判断字段已全部迁到 library_papers（迁移 A + 代码切换）：删除 papers 上的
project_id 与判断列、paper_chunks 冗余 project_id；concepts 完成
project_id → library_id（按隐式库映射搬迁，唯一约束随迁）。

Revision ID: fe8a86942dc7
Revises: f7c2abfe8aeb
Create Date: 2026-07-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "fe8a86942dc7"
down_revision: str | None = "f7c2abfe8aeb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # ---- concepts: project_id → library_id ----
    op.add_column("concepts", sa.Column("library_id", sa.Uuid(), nullable=True))
    bind.execute(
        sa.text(
            "UPDATE concepts SET library_id = ("
            "SELECT dl.id FROM direction_libraries dl WHERE dl.project_id = concepts.project_id"
            ")"
        )
    )
    # 无对应隐式库的孤儿概念（理论上不存在）直接清掉，保证 NOT NULL 收紧
    bind.execute(sa.text("DELETE FROM concepts WHERE library_id IS NULL"))
    op.drop_index("ix_concepts_project_id", table_name="concepts")
    with op.batch_alter_table("concepts") as batch:
        batch.drop_constraint("uq_concepts_project_slug", type_="unique")
        batch.alter_column("library_id", existing_type=sa.Uuid(), nullable=False)
        batch.create_foreign_key(
            "fk_concepts_library_id",
            "direction_libraries",
            ["library_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint("uq_concepts_library_slug", ["library_id", "slug"])
        batch.drop_column("project_id")
    op.create_index("ix_concepts_library_id", "concepts", ["library_id"])

    # ---- paper_chunks: 删冗余 project_id ----
    op.drop_index("ix_paper_chunks_project_id", table_name="paper_chunks")
    with op.batch_alter_table("paper_chunks") as batch:
        batch.drop_column("project_id")

    # ---- papers: 删 project_id 与已迁走的判断列 ----
    op.drop_index("ix_papers_project_id", table_name="papers")
    with op.batch_alter_table("papers") as batch:
        batch.drop_column("project_id")
        batch.drop_column("relevance_score")
        batch.drop_column("wiki_content")
        batch.drop_column("status")
        batch.drop_column("trash_reason")
        batch.drop_column("scored_at")
        batch.drop_column("compiled_at")
        batch.drop_column("compiled_model")


def downgrade() -> None:
    # papers 判断列还原（数据不回搬——P4 后成员表才是权威，此处仅结构回滚）
    with op.batch_alter_table("papers") as batch:
        batch.add_column(sa.Column("compiled_model", sa.String(255), nullable=True))
        batch.add_column(sa.Column("compiled_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("trash_reason", sa.String(16), nullable=True))
        batch.add_column(
            sa.Column("status", sa.String(32), nullable=False, server_default="candidate")
        )
        batch.add_column(sa.Column("wiki_content", sa.Text(), nullable=True))
        batch.add_column(sa.Column("relevance_score", sa.Float(), nullable=True))
        batch.add_column(sa.Column("project_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_papers_project_id", "projects", ["project_id"], ["id"], ondelete="CASCADE"
        )
    op.create_index("ix_papers_project_id", "papers", ["project_id"])

    with op.batch_alter_table("paper_chunks") as batch:
        batch.add_column(sa.Column("project_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_paper_chunks_project_id", "projects", ["project_id"], ["id"], ondelete="CASCADE"
        )
    op.create_index("ix_paper_chunks_project_id", "paper_chunks", ["project_id"])

    op.drop_index("ix_concepts_library_id", table_name="concepts")
    with op.batch_alter_table("concepts") as batch:
        batch.add_column(sa.Column("project_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_concepts_project_id", "projects", ["project_id"], ["id"], ondelete="CASCADE"
        )
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE concepts SET project_id = ("
            "SELECT dl.project_id FROM direction_libraries dl WHERE dl.id = concepts.library_id"
            ")"
        )
    )
    with op.batch_alter_table("concepts") as batch:
        batch.drop_constraint("uq_concepts_library_slug", type_="unique")
        batch.drop_constraint("fk_concepts_library_id", type_="foreignkey")
        batch.create_unique_constraint("uq_concepts_project_slug", ["project_id", "slug"])
        batch.drop_column("library_id")
    op.create_index("ix_concepts_project_id", "concepts", ["project_id"])
