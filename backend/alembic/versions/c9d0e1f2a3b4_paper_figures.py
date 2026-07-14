"""paper figures: papers.figures JSON column per docs/api-lit.md §6.5

figures 结构：[{index, page, width, height, caption: str|null, important: bool}]，
图片文件落 <data_dir>/papers/<paper_id>/figures/fig_<index>.png（路径不入库）。

Revision ID: c9d0e1f2a3b4
Revises: b7c8d9e0f1a2
Create Date: 2026-07-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b7c8d9e0f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# postgres 用 JSONB，sqlite 等回退通用 JSON（与 app.models.base.JSONVariant 一致）
JSONVariant = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column("papers", sa.Column("figures", JSONVariant, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("papers") as batch:
        batch.drop_column("figures")
