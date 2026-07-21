"""user_library_entries.wiki_content snapshot

Revision ID: 8b3b904e7588
Revises: a1c7e93f5b02
Create Date: 2026-07-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b3b904e7588"
down_revision: str | None = "a1c7e93f5b02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_library_entries", sa.Column("wiki_content", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_library_entries", "wiki_content")
