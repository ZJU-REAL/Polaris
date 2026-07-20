"""user username unique

Revision ID: 183e8de0e85f
Revises: 9f2a7c04b6e1
Create Date: 2026-07-19 23:29:07.920234

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "183e8de0e85f"
down_revision: str | None = "9f2a7c04b6e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 用户名：可空（老用户无），唯一索引（多个 NULL 互不冲突）。
    op.add_column("users", sa.Column("username", sa.String(length=32), nullable=True))
    op.create_index("ix_users_username", "users", ["username"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_username", table_name="users")
    op.drop_column("users", "username")
