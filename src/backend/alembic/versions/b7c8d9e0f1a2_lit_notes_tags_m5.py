"""literature enhance (M5): paper notes / tags / per-user meta per docs/api-lit.md

- paper_notes：论文笔记（paper/project/author 外键，content 正文）
- paper_tags：项目级标签（uq(project, name)）+ paper_tag_links 多对多关联
- paper_user_meta：个人星标 / 阅读状态（uq(paper, user)）

Revision ID: b7c8d9e0f1a2
Revises: a9b0c1d2e3f4
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: str | None = "a9b0c1d2e3f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_notes",
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_paper_notes_paper_id"), "paper_notes", ["paper_id"], unique=False)
    op.create_index(op.f("ix_paper_notes_project_id"), "paper_notes", ["project_id"], unique=False)
    op.create_index(op.f("ix_paper_notes_author_id"), "paper_notes", ["author_id"], unique=False)

    op.create_table(
        "paper_tags",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "name", name="uq_paper_tags_project_name"),
    )
    op.create_index(op.f("ix_paper_tags_project_id"), "paper_tags", ["project_id"], unique=False)

    op.create_table(
        "paper_tag_links",
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("tag_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tag_id"], ["paper_tags.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("paper_id", "tag_id"),
    )

    op.create_table(
        "paper_user_meta",
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("starred", sa.Boolean(), nullable=False),
        sa.Column("reading_status", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("paper_id", "user_id", name="uq_paper_user_meta"),
    )
    op.create_index(
        op.f("ix_paper_user_meta_paper_id"), "paper_user_meta", ["paper_id"], unique=False
    )
    op.create_index(
        op.f("ix_paper_user_meta_user_id"), "paper_user_meta", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_paper_user_meta_user_id"), table_name="paper_user_meta")
    op.drop_index(op.f("ix_paper_user_meta_paper_id"), table_name="paper_user_meta")
    op.drop_table("paper_user_meta")
    op.drop_table("paper_tag_links")
    op.drop_index(op.f("ix_paper_tags_project_id"), table_name="paper_tags")
    op.drop_table("paper_tags")
    op.drop_index(op.f("ix_paper_notes_author_id"), table_name="paper_notes")
    op.drop_index(op.f("ix_paper_notes_project_id"), table_name="paper_notes")
    op.drop_index(op.f("ix_paper_notes_paper_id"), table_name="paper_notes")
    op.drop_table("paper_notes")
