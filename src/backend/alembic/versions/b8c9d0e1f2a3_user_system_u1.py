"""user system U1: 用户头像/配额/功能权限 + 研究方向邀请链接

- users：+avatar_path（头像文件路径）、+token_quota（LLM token 配额，None=不限）、
  +features（功能权限 JSON，缺省=全部允许）、+llm_access（大模型使用权限三档）
- project_invites：邀请链接（token 唯一；过期时间/最大使用次数/撤销）

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-07-16
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("avatar_path", sa.String(length=1024), nullable=True))
    op.add_column("users", sa.Column("token_quota", sa.BigInteger(), nullable=True))
    op.add_column("users", sa.Column("features", sa.JSON(), nullable=True))
    op.add_column(
        "users",
        sa.Column("llm_access", sa.String(length=16), nullable=False, server_default="full"),
    )

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


def downgrade() -> None:
    op.drop_index("ix_project_invites_token", table_name="project_invites")
    op.drop_index("ix_project_invites_project_id", table_name="project_invites")
    op.drop_table("project_invites")
    op.drop_column("users", "llm_access")
    op.drop_column("users", "features")
    op.drop_column("users", "token_quota")
    op.drop_column("users", "avatar_path")
