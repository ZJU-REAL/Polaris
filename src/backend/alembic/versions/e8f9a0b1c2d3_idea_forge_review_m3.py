"""idea forge & review (M3): idea scoring/elo/embedding columns, review session payload, message author_name

- ideas：score_rationale JSON、matches/wins 计数、embedding（pg pgvector(1024)，sqlite JSON）
- review_sessions：payload JSON（idea_match 的 idea_a/idea_b/round/winner）
- review_messages：author_name（人设名或用户 display_name，替代 agent_persona）

Revision ID: e8f9a0b1c2d3
Revises: d6e7f8a9b0c1
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e8f9a0b1c2d3"
down_revision: str | None = "d6e7f8a9b0c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONVariant = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
# postgres 用 pgvector（语义去重），sqlite 等回退 JSON 存 list（同 papers.embedding）
EmbeddingVariant = sa.JSON().with_variant(Vector(1024), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    with op.batch_alter_table("ideas") as batch:
        batch.add_column(sa.Column("score_rationale", JSONVariant, nullable=True))
        batch.add_column(
            sa.Column("matches", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("wins", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("embedding", EmbeddingVariant, nullable=True))

    with op.batch_alter_table("review_sessions") as batch:
        batch.add_column(sa.Column("payload", JSONVariant, nullable=True))

    with op.batch_alter_table("review_messages") as batch:
        batch.add_column(sa.Column("author_name", sa.String(length=255), nullable=True))
        batch.drop_column("agent_persona")


def downgrade() -> None:
    with op.batch_alter_table("review_messages") as batch:
        batch.add_column(sa.Column("agent_persona", sa.String(length=64), nullable=True))
        batch.drop_column("author_name")

    with op.batch_alter_table("review_sessions") as batch:
        batch.drop_column("payload")

    with op.batch_alter_table("ideas") as batch:
        batch.drop_column("embedding")
        batch.drop_column("wins")
        batch.drop_column("matches")
        batch.drop_column("score_rationale")
