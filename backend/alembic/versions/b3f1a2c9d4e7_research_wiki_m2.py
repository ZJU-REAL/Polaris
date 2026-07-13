"""research wiki (M2): paper ingest columns + pgvector embedding, concept scoping, project ingest_state

Revision ID: b3f1a2c9d4e7
Revises: fc80e67139bc
Create Date: 2026-07-13 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f1a2c9d4e7"
down_revision: str | None = "fc80e67139bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONVariant = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")
# postgres 用 pgvector；sqlite 等回退 JSON 存 list
EmbeddingVariant = sa.JSON().with_variant(Vector(1536), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # papers：ingest 元数据 + 全文/嵌入列
    with op.batch_alter_table("papers") as batch:
        batch.add_column(sa.Column("source", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("external_ids", JSONVariant, nullable=True))
        batch.add_column(sa.Column("published_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("full_text_path", sa.String(length=1024), nullable=True))
        batch.add_column(sa.Column("tldr", sa.Text(), nullable=True))
        batch.add_column(sa.Column("embedding", EmbeddingVariant, nullable=True))
        batch.add_column(sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("compiled_at", sa.DateTime(timezone=True), nullable=True))

    # concepts：按项目隔离 + slug（M2 前表为空，NOT NULL 可直接加）
    with op.batch_alter_table("concepts") as batch:
        batch.add_column(sa.Column("project_id", sa.Uuid(), nullable=False))
        batch.add_column(sa.Column("slug", sa.String(length=255), nullable=False))
        batch.create_foreign_key(
            "fk_concepts_project_id_projects", "projects", ["project_id"], ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint("uq_concepts_project_slug", ["project_id", "slug"])
    op.create_index(op.f("ix_concepts_project_id"), "concepts", ["project_id"], unique=False)
    op.create_index(op.f("ix_concepts_slug"), "concepts", ["slug"], unique=False)

    # projects：ingest 水位线状态
    op.add_column("projects", sa.Column("ingest_state", JSONVariant, nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "ingest_state")

    op.drop_index(op.f("ix_concepts_slug"), table_name="concepts")
    op.drop_index(op.f("ix_concepts_project_id"), table_name="concepts")
    with op.batch_alter_table("concepts") as batch:
        batch.drop_constraint("uq_concepts_project_slug", type_="unique")
        batch.drop_constraint("fk_concepts_project_id_projects", type_="foreignkey")
        batch.drop_column("slug")
        batch.drop_column("project_id")

    with op.batch_alter_table("papers") as batch:
        batch.drop_column("compiled_at")
        batch.drop_column("scored_at")
        batch.drop_column("embedding")
        batch.drop_column("tldr")
        batch.drop_column("full_text_path")
        batch.drop_column("published_at")
        batch.drop_column("external_ids")
        batch.drop_column("source")
