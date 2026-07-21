"""ideas/experiments 垃圾箱 trashed_at + 把 manuscripts trashed_at/pinned_at 修成带时区

- ideas.trashed_at / experiments.trashed_at（timestamptz，软删除）
- manuscripts.trashed_at / pinned_at：原来误建成 naive DateTime，Postgres 下写入
  tz-aware UTC 会报「can't subtract offset-naive and offset-aware」——改成 timestamptz
  （仅当当前还是 naive 时执行，幂等）

Revision ID: 7a2c9e4f16b3
Revises: 5b1f3a9c72e6
Create Date: 2026-07-20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7a2c9e4f16b3"
down_revision: str | None = "5b1f3a9c72e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_naive(bind, table: str, col: str) -> bool:
    dt = bind.execute(
        sa.text(
            "select data_type from information_schema.columns "
            "where table_name=:t and column_name=:c"
        ),
        {"t": table, "c": col},
    ).scalar()
    return dt == "timestamp without time zone"


def upgrade() -> None:
    op.add_column("ideas", sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("experiments", sa.Column("trashed_at", sa.DateTime(timezone=True), nullable=True))
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for col in ("trashed_at", "pinned_at"):
            if _is_naive(bind, "manuscripts", col):
                op.alter_column(
                    "manuscripts",
                    col,
                    type_=sa.DateTime(timezone=True),
                    postgresql_using=f"{col} AT TIME ZONE 'UTC'",
                )


def downgrade() -> None:
    op.drop_column("experiments", "trashed_at")
    op.drop_column("ideas", "trashed_at")
