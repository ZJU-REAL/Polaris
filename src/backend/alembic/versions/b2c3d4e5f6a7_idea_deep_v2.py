"""idea 2.0: ideas 表深耕字段 per docs/api-idea2.md §7

- depth VARCHAR(16) NOT NULL DEFAULT 'sketch'：历史数据一律 sketch
- research_type / goal / evidence / seed_idea_id：深耕产物（Research Proposal）专属，可空

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONVariant = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("ideas") as batch:
        batch.add_column(
            sa.Column("depth", sa.String(length=16), nullable=False, server_default="sketch")
        )
        batch.add_column(sa.Column("research_type", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("goal", JSONVariant, nullable=True))
        batch.add_column(sa.Column("evidence", JSONVariant, nullable=True))
        batch.add_column(sa.Column("seed_idea_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_ideas_seed_idea_id", "ideas", ["seed_idea_id"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("ideas") as batch:
        batch.drop_constraint("fk_ideas_seed_idea_id", type_="foreignkey")
        batch.drop_column("seed_idea_id")
        batch.drop_column("evidence")
        batch.drop_column("goal")
        batch.drop_column("research_type")
        batch.drop_column("depth")
