"""manuscript templates + binary/folder files: 模板 DB 化 + 稿件文件支持二进制/文件夹

- manuscript_templates 表：上传/种子模板元数据（文件落 data_dir/templates/<id>/files）
- manuscript_files：+is_binary（二进制资源字节落磁盘）+is_folder（文件夹占位）

Revision ID: a5b6c7d8e9f0
Revises: e4f5a6b7c8d9
Create Date: 2026-07-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a5b6c7d8e9f0"
down_revision: str | None = "e4f5a6b7c8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONVariant = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.create_table(
        "manuscript_templates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("key", sa.String(length=96), nullable=False),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("description", sa.String(length=1024), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="global"),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("main_tex", sa.String(length=512), nullable=False, server_default="main.tex"),
        sa.Column("engine", sa.String(length=16), nullable=False, server_default="tectonic"),
        sa.Column("page_limit", sa.Integer(), nullable=True),
        sa.Column("sections", JSONVariant, nullable=True),
        sa.Column("unofficial", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("meta", JSONVariant, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_manuscript_templates_key", "manuscript_templates", ["key"], unique=True
    )
    op.create_index(
        "ix_manuscript_templates_project_id", "manuscript_templates", ["project_id"]
    )

    with op.batch_alter_table("manuscript_files") as batch:
        batch.add_column(
            sa.Column("is_binary", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(
            sa.Column("is_folder", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("manuscript_files") as batch:
        batch.drop_column("is_folder")
        batch.drop_column("is_binary")
    op.drop_index("ix_manuscript_templates_project_id", table_name="manuscript_templates")
    op.drop_index("ix_manuscript_templates_key", table_name="manuscript_templates")
    op.drop_table("manuscript_templates")
