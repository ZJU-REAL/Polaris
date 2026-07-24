"""删除惰性列 projects.ingest_state（P9e）

P8/P9c 后水位线权威源在 ``DirectionLibrary.ingest_state``，``projects.ingest_state``
已无任何读写者（inert）。此迁移删列；downgrade 仅结构还原（不回搬数据）。

Revision ID: c6e0b3a91f47
Revises: a4d21f8c7b09
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c6e0b3a91f47"
down_revision: str | None = "a4d21f8c7b09"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONVariant = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("ingest_state")


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.add_column(sa.Column("ingest_state", JSONVariant, nullable=True))
