"""课题一句话上列 + 退役 project.definition（P9e）

P9c 后 ``project.definition`` 只装 ``{"statement": ...}``。此迁移给 ``projects`` 加
``statement`` Text 列，把 ``definition->>'statement'`` 拷进新列，然后删除 ``definition``
列。收录配置权威源始终在文献库 ``DirectionLibrary.definition``，不受影响。

Revision ID: e2b9d47a0c31
Revises: c6e0b3a91f47
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e2b9d47a0c31"
down_revision: str | None = "c6e0b3a91f47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONVariant = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column("projects", sa.Column("statement", sa.Text(), nullable=True))
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "UPDATE projects SET statement = definition->>'statement' "
                "WHERE definition IS NOT NULL"
            )
        )
    else:
        bind.execute(
            sa.text(
                "UPDATE projects SET statement = json_extract(definition, '$.statement') "
                "WHERE definition IS NOT NULL"
            )
        )
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("definition")


def downgrade() -> None:
    bind = op.get_bind()
    op.add_column("projects", sa.Column("definition", JSONVariant, nullable=True))
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text(
                "UPDATE projects SET definition = jsonb_build_object('statement', statement) "
                "WHERE statement IS NOT NULL"
            )
        )
    else:
        bind.execute(
            sa.text(
                "UPDATE projects SET definition = json_object('statement', statement) "
                "WHERE statement IS NOT NULL"
            )
        )
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("statement")
