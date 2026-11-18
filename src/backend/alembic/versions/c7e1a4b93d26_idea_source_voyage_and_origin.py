"""ideas.source_voyage_id / ideas.origin: where an idea came from

A hypothesis search now writes its surviving hypotheses as ideas. Two columns record
that link:

- ``source_voyage_id``: the task that wrote the idea (set to NULL when the task is
  deleted). Together with ``origin.node_id`` it makes the write idempotent on replay.
- ``origin``: JSON describing the source, e.g. the hypothesis node, its evidence
  ratios, and ``novelty_scope: "library"`` (novelty was checked against the library
  only, never against the outside world).

Both are nullable; existing ideas keep NULL.

Revision ID: c7e1a4b93d26
Revises: c7a1e4d9b3f2
Create Date: 2026-11-18 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c7e1a4b93d26"
down_revision: str | None = "c7a1e4d9b3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    # batch: SQLite cannot add a foreign key with ALTER TABLE
    with op.batch_alter_table("ideas") as batch:
        batch.add_column(sa.Column("source_voyage_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("origin", _JSON, nullable=True))
        batch.create_foreign_key(
            "fk_ideas_source_voyage_id_voyage_runs",
            "voyage_runs",
            ["source_voyage_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_ideas_source_voyage_id", ["source_voyage_id"])


def downgrade() -> None:
    with op.batch_alter_table("ideas") as batch:
        batch.drop_index("ix_ideas_source_voyage_id")
        batch.drop_constraint("fk_ideas_source_voyage_id_voyage_runs", type_="foreignkey")
        batch.drop_column("origin")
        batch.drop_column("source_voyage_id")
