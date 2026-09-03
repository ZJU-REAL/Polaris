"""移除应用内反馈两表 feedback / feedback_images（#617）

反馈改为前端直接打开预填好的 GitHub new-issue 页面：截图由用户在 GitHub
页面粘贴，triage 直接在 issue 列表里做。应用内的提交/截图/管理端管线
（含 LLM 起草与后端代建 issue）整体退役，两张表一并删除。

Revision ID: 9b2e5d81c7a3
Revises: 4f8d2c9b7a61
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9b2e5d81c7a3"
down_revision: str | None = "4f8d2c9b7a61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 子表先删（FK 指向 feedback）
    op.drop_index("ix_feedback_images_feedback_id", table_name="feedback_images")
    op.drop_table("feedback_images")
    op.drop_index("ix_feedback_user_id", table_name="feedback")
    op.drop_table("feedback")


def downgrade() -> None:
    # 复原 b3f1a7c92e5d 的最终形状（数据不可恢复；截图文件本就在盘上、不入库）。
    op.create_table(
        "feedback",
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(length=16), nullable=False, server_default="bug"),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="normal"),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("route", sa.String(length=255), nullable=True),
        sa.Column("module", sa.String(length=64), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="new"),
        sa.Column("admin_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("issue_draft", sa.JSON(), nullable=True),
        sa.Column("github_issue_number", sa.Integer(), nullable=True),
        sa.Column("github_issue_url", sa.String(length=255), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feedback_user_id", "feedback", ["user_id"])
    op.create_table(
        "feedback_images",
        sa.Column("feedback_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.String(length=512), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["feedback_id"], ["feedback.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_feedback_images_feedback_id", "feedback_images", ["feedback_id"])
