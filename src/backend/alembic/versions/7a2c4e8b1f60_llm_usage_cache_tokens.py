"""llm_usage: record cache-read and cache-write input tokens separately

With prompt caching on, part of every prompt is billed at 0.1x (read from cache) or
1.25x (written to cache). ``prompt_tokens`` stays the total sent; these two columns say
how much of it was cached, so the cost shown to the user can apply the discount and the
cache hit rate of a run can be measured.

Revision ID: 7a2c4e8b1f60
Revises: 5e1b7c9a4d32
Create Date: 2026-11-18 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7a2c4e8b1f60"
down_revision: str | None = "5e1b7c9a4d32"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_usage") as batch:
        batch.add_column(
            sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("cache_write_tokens", sa.Integer(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("llm_usage") as batch:
        batch.drop_column("cache_write_tokens")
        batch.drop_column("cache_read_tokens")
