"""per-user LLM config: owner_id on providers/routes + users.llm_self_managed

- users.llm_self_managed（False=被管理员接管，用全局配置；True=自管）
- llm_providers.owner_id / model_routes.owner_id（NULL=全局，<user>=私有）
- 全局唯一 name/stage 改为按 owner 分别唯一（两条部分唯一索引）

Revision ID: a1c7e93f5b02
Revises: 257c979a4b99
Create Date: 2026-07-21
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c7e93f5b02"
down_revision: str | None = "257c979a4b99"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _add_owner_and_drop_unique(table: str, uniq_col: str, is_pg: bool) -> None:
    """给表加 owner_id（FK→users, CASCADE）并删掉旧的全局单列唯一约束。"""
    if is_pg:
        op.add_column(table, sa.Column("owner_id", sa.Uuid(), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_owner", table, "users", ["owner_id"], ["id"], ondelete="CASCADE"
        )
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])
        conname = (
            op.get_bind()
            .execute(
                sa.text(
                    "select c.conname from pg_constraint c "
                    "join pg_class rel on rel.oid = c.conrelid "
                    "where rel.relname = :t and c.contype = 'u' "
                    "and array_length(c.conkey, 1) = 1"
                ),
                {"t": table},
            )
            .scalar()
        )
        if conname:
            op.drop_constraint(conname, table, type_="unique")
    else:
        with op.batch_alter_table(table, naming_convention=_NAMING) as b:
            b.add_column(sa.Column("owner_id", sa.Uuid(), nullable=True))
            b.create_foreign_key(
                f"fk_{table}_owner", "users", ["owner_id"], ["id"], ondelete="CASCADE"
            )
            b.drop_constraint(f"uq_{table}_{uniq_col}", type_="unique")
        op.create_index(f"ix_{table}_owner_id", table, ["owner_id"])


def _partial_unique(name: str, table: str, cols: list[str], global_scope: bool) -> None:
    where = "owner_id IS NULL" if global_scope else "owner_id IS NOT NULL"
    op.create_index(
        name,
        table,
        cols,
        unique=True,
        postgresql_where=sa.text(where),
        sqlite_where=sa.text(where),
    )


def upgrade() -> None:
    is_pg = op.get_bind().dialect.name == "postgresql"
    op.add_column(
        "users",
        sa.Column("llm_self_managed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    _add_owner_and_drop_unique("llm_providers", "name", is_pg)
    _add_owner_and_drop_unique("model_routes", "stage", is_pg)
    # 按 owner 分别唯一：全局(owner NULL) 与 每用户各自唯一
    _partial_unique("uq_providers_global_name", "llm_providers", ["name"], True)
    _partial_unique("uq_providers_owner_name", "llm_providers", ["owner_id", "name"], False)
    _partial_unique("uq_routes_global_stage", "model_routes", ["stage"], True)
    _partial_unique("uq_routes_owner_stage", "model_routes", ["owner_id", "stage"], False)


def downgrade() -> None:
    is_pg = op.get_bind().dialect.name == "postgresql"
    for name in (
        "uq_providers_global_name",
        "uq_providers_owner_name",
        "uq_routes_global_stage",
        "uq_routes_owner_stage",
    ):
        op.drop_index(name)
    for table, uniq_col in (("llm_providers", "name"), ("model_routes", "stage")):
        if is_pg:
            op.drop_index(f"ix_{table}_owner_id", table_name=table)
            op.drop_constraint(f"fk_{table}_owner", table, type_="foreignkey")
            op.drop_column(table, "owner_id")
            op.create_unique_constraint(f"uq_{table}_{uniq_col}", table, [uniq_col])
        else:
            op.drop_index(f"ix_{table}_owner_id", table_name=table)
            with op.batch_alter_table(table, naming_convention=_NAMING) as b:
                b.drop_constraint(f"fk_{table}_owner", type_="foreignkey")
                b.drop_column("owner_id")
                b.create_unique_constraint(f"uq_{table}_{uniq_col}", [uniq_col])
    op.drop_column("users", "llm_self_managed")
