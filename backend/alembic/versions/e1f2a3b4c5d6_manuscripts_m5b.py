"""manuscripts m5b: writing workbench columns per docs/api-m5-b.md

- manuscripts.experiment_id FK(SET NULL)：关联实验（fact-pack 事实源）
- manuscripts.template：模板 pack key（neurips2026 | iclr2026 | acl）
- manuscripts.fact_pack JSON：防幻觉事实包
- manuscripts.latest_compile JSON：最近一次 CompileResult
- manuscript_files.readonly BOOL：模板样式文件只读标记
- manuscript_files.updated_by FK(SET NULL)：最后编辑人（REST 写入时记录）

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# postgres 用 JSONB，sqlite 等回退通用 JSON（与 app.models.base.JSONVariant 一致）
JSONVariant = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("manuscripts") as batch:
        batch.add_column(sa.Column("experiment_id", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column(
                "template", sa.String(length=64), nullable=False, server_default="neurips2026"
            )
        )
        batch.add_column(sa.Column("fact_pack", JSONVariant, nullable=True))
        batch.add_column(sa.Column("latest_compile", JSONVariant, nullable=True))
        batch.create_foreign_key(
            "fk_manuscripts_experiment_id",
            "experiments",
            ["experiment_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        op.f("ix_manuscripts_experiment_id"), "manuscripts", ["experiment_id"], unique=False
    )

    with op.batch_alter_table("manuscript_files") as batch:
        batch.add_column(
            sa.Column("readonly", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("updated_by", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_manuscript_files_updated_by", "users", ["updated_by"], ["id"], ondelete="SET NULL"
        )


def downgrade() -> None:
    with op.batch_alter_table("manuscript_files") as batch:
        batch.drop_constraint("fk_manuscript_files_updated_by", type_="foreignkey")
        batch.drop_column("updated_by")
        batch.drop_column("readonly")

    op.drop_index(op.f("ix_manuscripts_experiment_id"), table_name="manuscripts")
    with op.batch_alter_table("manuscripts") as batch:
        batch.drop_constraint("fk_manuscripts_experiment_id", type_="foreignkey")
        batch.drop_column("latest_compile")
        batch.drop_column("fact_pack")
        batch.drop_column("template")
        batch.drop_column("experiment_id")
