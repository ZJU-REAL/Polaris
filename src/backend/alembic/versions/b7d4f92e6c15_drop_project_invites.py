"""移除邀请链接表 project_invites（实验室成员招募机制，个人化定位下撤除）

Revision ID: b7d4f92e6c15
Revises: a3e9c17b5f42
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7d4f92e6c15"
down_revision: str | None = "a3e9c17b5f42"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_project_invites_token", table_name="project_invites")
    op.drop_index("ix_project_invites_project_id", table_name="project_invites")
    op.drop_table("project_invites")


def downgrade() -> None:
    # 复原 b8c9d0e1f2a3 的形状（数据不可恢复）。
    op.create_table(
        "project_invites",
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("used_count", sa.Integer(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_project_invites_project_id", "project_invites", ["project_id"])
    op.create_index("ix_project_invites_token", "project_invites", ["token"], unique=True)
