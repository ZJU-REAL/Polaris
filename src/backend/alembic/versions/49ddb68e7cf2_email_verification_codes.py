"""邮箱验证码表 email_verification_codes（注册验证 / 找回密码）

- email / purpose(register|reset) / code_hash / expires_at / attempts / consumed_at

Revision ID: 49ddb68e7cf2
Revises: f2d34705e152
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "49ddb68e7cf2"
down_revision: str | None = "f2d34705e152"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
    op.create_index(
        "ix_email_verification_codes_email", "email_verification_codes", ["email"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_email_verification_codes_email", table_name="email_verification_codes"
    )
    op.drop_table("email_verification_codes")
