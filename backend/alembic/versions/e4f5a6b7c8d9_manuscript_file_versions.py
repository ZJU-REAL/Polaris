"""manuscript file versions: 稿件文件版本快照（AI 写入前 / 编译当刻 / 恢复前备份）

Revision ID: e4f5a6b7c8d9
Revises: f4a5b6c7d8e9
Create Date: 2026-07-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: str | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "manuscript_file_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "file_id",
            sa.Uuid(),
            sa.ForeignKey("manuscript_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=256), nullable=True),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_manuscript_file_versions_file_id", "manuscript_file_versions", ["file_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_manuscript_file_versions_file_id", table_name="manuscript_file_versions")
    op.drop_table("manuscript_file_versions")
