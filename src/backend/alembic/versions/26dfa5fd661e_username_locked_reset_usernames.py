"""username_locked + reset usernames

Revision ID: 26dfa5fd661e
Revises: 183e8de0e85f
Create Date: 2026-07-20 00:51:14.175002

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "26dfa5fd661e"
down_revision: str | None = "183e8de0e85f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("username_locked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # 用户名系统正式上线的一次性重置：清空历史（测试期）用户名，
    # 首个管理员用户名设为 'admin' 并锁定；其余用户留空、可在设置里自行设定一次。
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE users SET username = NULL, username_locked = false"))
    admin_id = conn.execute(
        sa.text("SELECT id FROM users WHERE role = 'admin' ORDER BY created_at LIMIT 1")
    ).scalar()
    if admin_id is not None:
        conn.execute(
            sa.text("UPDATE users SET username = 'admin', username_locked = true WHERE id = :i"),
            {"i": admin_id},
        )


def downgrade() -> None:
    op.drop_column("users", "username_locked")
