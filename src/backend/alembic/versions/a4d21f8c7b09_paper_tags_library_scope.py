"""论文标签库化（P9e）：paper_tags.project_id → library_id

标签作用域从课题（project_id）改为文献库（library_id，FK→direction_libraries，
NOT NULL），唯一约束 (project_id,name) → (library_id,name)。数据迁移：现有 tag 的
project_id 映射到其起源隐式库（direction_libraries.project_id 反查）的 library_id；
无对应库的孤儿 tag（连同关联）删除。paper_tag_links 结构不变。

Revision ID: a4d21f8c7b09
Revises: d3a7f1c9b2e4
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4d21f8c7b09"
down_revision: str | None = "d3a7f1c9b2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column("paper_tags", sa.Column("library_id", sa.Uuid(), nullable=True))
    # 起源隐式库回填：direction_libraries.project_id == paper_tags.project_id
    bind.execute(
        sa.text(
            "UPDATE paper_tags SET library_id = ("
            "SELECT dl.id FROM direction_libraries dl WHERE dl.project_id = paper_tags.project_id"
            ")"
        )
    )
    # 无对应隐式库的孤儿标签：先删关联再删标签，保证 NOT NULL 收紧
    bind.execute(
        sa.text(
            "DELETE FROM paper_tag_links WHERE tag_id IN "
            "(SELECT id FROM paper_tags WHERE library_id IS NULL)"
        )
    )
    bind.execute(sa.text("DELETE FROM paper_tags WHERE library_id IS NULL"))

    op.drop_index("ix_paper_tags_project_id", table_name="paper_tags")
    with op.batch_alter_table("paper_tags") as batch:
        batch.drop_constraint("uq_paper_tags_project_name", type_="unique")
        batch.alter_column("library_id", existing_type=sa.Uuid(), nullable=False)
        batch.create_foreign_key(
            "fk_paper_tags_library_id",
            "direction_libraries",
            ["library_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint("uq_paper_tags_library_name", ["library_id", "name"])
        batch.drop_column("project_id")
    op.create_index("ix_paper_tags_library_id", "paper_tags", ["library_id"])


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_index("ix_paper_tags_library_id", table_name="paper_tags")
    with op.batch_alter_table("paper_tags") as batch:
        batch.add_column(sa.Column("project_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_paper_tags_project_id", "projects", ["project_id"], ["id"], ondelete="CASCADE"
        )
    # 库回指其起源课题；独立库（project_id 为空）的标签无处安放 → 删
    bind.execute(
        sa.text(
            "UPDATE paper_tags SET project_id = ("
            "SELECT dl.project_id FROM direction_libraries dl WHERE dl.id = paper_tags.library_id"
            ")"
        )
    )
    bind.execute(
        sa.text(
            "DELETE FROM paper_tag_links WHERE tag_id IN "
            "(SELECT id FROM paper_tags WHERE project_id IS NULL)"
        )
    )
    bind.execute(sa.text("DELETE FROM paper_tags WHERE project_id IS NULL"))

    with op.batch_alter_table("paper_tags") as batch:
        batch.drop_constraint("uq_paper_tags_library_name", type_="unique")
        batch.drop_constraint("fk_paper_tags_library_id", type_="foreignkey")
        batch.alter_column("project_id", existing_type=sa.Uuid(), nullable=False)
        batch.create_unique_constraint("uq_paper_tags_project_name", ["project_id", "name"])
        batch.drop_column("library_id")
    op.create_index("ix_paper_tags_project_id", "paper_tags", ["project_id"])
