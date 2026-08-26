"""Persist interdisciplinary retrieval channels and merge the two feature heads."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: str = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column("interdisciplinary_research_profiles", sa.Column("query_matrix", _JSON))
    op.add_column("interdisciplinary_research_profiles", sa.Column("evidence_balance", _JSON))


def downgrade() -> None:
    op.drop_column("interdisciplinary_research_profiles", "evidence_balance")
    op.drop_column("interdisciplinary_research_profiles", "query_matrix")
