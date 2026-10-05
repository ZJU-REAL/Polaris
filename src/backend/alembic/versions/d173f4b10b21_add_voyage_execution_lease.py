"""add voyage execution lease

Revision ID: d173f4b10b21
Revises: d3f9a1c7e2b4
Create Date: 2026-10-04 19:02:42.048252

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d173f4b10b21"
down_revision: str | None = "d3f9a1c7e2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("voyage_runs", sa.Column("execution_token", sa.Uuid(), nullable=True))
    op.add_column(
        "voyage_runs", sa.Column("execution_expires_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("voyage_runs", "execution_expires_at")
    op.drop_column("voyage_runs", "execution_token")
