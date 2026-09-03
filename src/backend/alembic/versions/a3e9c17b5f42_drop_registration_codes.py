"""移除注册码表 registration_codes（实验室招新机制，个人化定位下撤除）

注册仍由静态 settings.invite_code 把门（A3 单用户免登录后一并退役）。

Revision ID: a3e9c17b5f42
Revises: f4a5b6c7d8e9
Create Date: 2026-09-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3e9c17b5f42"
down_revision: str | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_registration_codes_code", table_name="registration_codes")
    op.drop_table("registration_codes")


def downgrade() -> None:
    # 复原 f7a1c3e59d24 + 94e6bc81c510 的最终形状（数据不可恢复）。
    op.create_table(
        "registration_codes",
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("note", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("max_uses", sa.Integer(), nullable=True),
        sa.Column("used_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("preset_directions", sa.JSON(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_registration_codes_code", "registration_codes", ["code"], unique=True)
