"""删掉邮箱验证码表（#842：去掉账号层）

Polaris 只在本机运行、只有一个本地用户：注册、邮箱验证码、找回密码都已删除，
email_verification_codes 不再有读写方。验证码只是短命的一次性数据，降级时建回
空表即可，不需要还原内容。

Revision ID: 88a5bdc0aa15
Revises: a9c4e7d2f1b8
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "88a5bdc0aa15"
down_revision: str | None = "a9c4e7d2f1b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_email_verification_codes_email", table_name="email_verification_codes")
    op.drop_table("email_verification_codes")


def downgrade() -> None:
    op.create_table(
        "email_verification_codes",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("purpose", sa.String(length=16), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_email_verification_codes_email", "email_verification_codes", ["email"])
