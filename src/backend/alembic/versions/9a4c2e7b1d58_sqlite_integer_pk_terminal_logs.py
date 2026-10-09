"""SQLite: voyage_terminal_logs.id becomes INTEGER PRIMARY KEY so it auto-increments

d800d34cd207 created ``voyage_terminal_logs.id`` as ``BIGINT``. On PostgreSQL that is
an identity column; on SQLite only a column declared exactly ``INTEGER PRIMARY KEY``
aliases the rowid and gets a value on insert. A ``BIGINT`` primary key is a plain
column, so every insert without an id failed with
``NOT NULL constraint failed: voyage_terminal_logs.id`` and no task terminal log was
ever saved on a migrated SQLite database (the model already falls back to Integer on
SQLite, which is why databases built from the metadata were fine).

This rebuilds the table on SQLite with ``id INTEGER``; existing rows, the run_id
index and the foreign key to voyage_runs are kept. It is the only table with an
integer surrogate key whose migrated type was not INTEGER (checked against the whole
upgraded schema). Nothing to do on other databases.

Revision ID: 9a4c2e7b1d58
Revises: 7d2e4f9a1b63
Create Date: 2026-10-09 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9a4c2e7b1d58"
down_revision: str | None = "7d2e4f9a1b63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    with op.batch_alter_table("voyage_terminal_logs", recreate="always") as batch:
        batch.alter_column(
            "id",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
            autoincrement=True,
        )


def downgrade() -> None:
    # 不退回 BIGINT：那样只会把「终端日志写不进去」重新带回来，INTEGER 在 SQLite 上
    # 本来就是 64 位。
    pass
