"""add compiled_model to papers

Revision ID: 877da11ed6b4
Revises: 09533b866a6d
Create Date: 2026-07-21 08:46:49.463027

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision: str = '877da11ed6b4'
down_revision: str | None = '09533b866a6d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("papers", sa.Column("compiled_model", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("papers", "compiled_model")
