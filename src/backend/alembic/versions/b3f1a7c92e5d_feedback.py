"""用户反馈表 feedback + feedback_images

- feedback：分类/严重度/标题/正文/route/module/context/status/admin_note
  + issue_draft(JSON) + github_issue_number/url
- feedback_images：截图路径（落盘 data_dir/feedback/<id>/）

Revision ID: b3f1a7c92e5d
Revises: c01ea43927b8
Create Date: 2026-07-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3f1a7c92e5d"
down_revision: str | None = "c01ea43927b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("ix_feedback_images_feedback_id", table_name="feedback_images")
    op.drop_table("feedback_images")
    op.drop_index("ix_feedback_user_id", table_name="feedback")
    op.drop_table("feedback")
