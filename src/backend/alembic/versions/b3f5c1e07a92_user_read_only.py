"""read-only (guest) accounts

Revision ID: b3f5c1e07a92
Revises: d4e8b19c7a55
Create Date: 2026-08-06

只读账号：能看的由 role 决定，能改的一律没有。存量用户全部不是只读——
默认 false，加列不动任何人的权限。
"""

import sqlalchemy as sa
from alembic import op

revision: str = "b3f5c1e07a92"
down_revision: str | None = "d4e8b19c7a55"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "read_only", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "read_only")
