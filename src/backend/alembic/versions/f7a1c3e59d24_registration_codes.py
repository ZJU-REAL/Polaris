"""注册码表 registration_codes（管理员生成的可控注册凭证）

- code（唯一）/ note / created_by / expires_at / max_uses / used_count / revoked

Revision ID: f7a1c3e59d24
Revises: 877da11ed6b4
Create Date: 2026-07-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f7a1c3e59d24"
down_revision: str | None = "877da11ed6b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "registration_codes",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_registration_codes_code", "registration_codes", ["code"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_registration_codes_code", table_name="registration_codes")
    op.drop_table("registration_codes")
