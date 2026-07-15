"""ssh_credentials.proxy_url — 服务器出外网代理（可空=直连）

Revision ID: a1b2c3d4e5f6
Revises: f3b4c5d6e7a8
Create Date: 2026-07-15
"""

import sqlalchemy as sa
from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "f3b4c5d6e7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ssh_credentials", sa.Column("proxy_url", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("ssh_credentials", "proxy_url")
