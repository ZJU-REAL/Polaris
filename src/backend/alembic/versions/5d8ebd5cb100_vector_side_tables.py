"""向量搬进侧表（paper_vectors / paper_chunk_vectors / idea_vectors）

向量从主表的一列搬到独立的侧表，每行带一个「向量空间」标识（``model@dim``）。
这样一个对象可以同时持有多个空间的向量，换嵌入模型就成了「后台预建新空间 →
改一下激活空间设置」，旧向量留在库里可随时切回；侧表的 ``embedding`` 列不带
维度，换 4096 维的模型也不需要再改表（issue #191）。

存量搬迁：旧列全是 1024 维，逐行按它自己的 ``embedding_model`` 归入
``<model>@1024``。这一列是后加的（c4e7b2a91f38），更早的行为 NULL——拿全局
路由表里配的嵌入模型名补上，那正是这些向量的实际出处。搬完按行数最多的空间
设为激活空间；一行向量都没有就不写设置，留给平台第一次嵌入时按模型实际返回的
维度自行初始化。

ideas 没有任何模型信息可依据，整体归入激活空间（它们与论文向量出自同一路由、
同一时期）。万一猜错，后果只是想法去重偶尔漏判，不会污染检索结果。

Revision ID: 5d8ebd5cb100
Revises: 510f6bde2233
Create Date: 2026-07-27 20:44:14.225962

"""
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5d8ebd5cb100'
down_revision: str | None = '510f6bde2233'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 旧列固定 1024 维（d6e7f8a9b0c1 起）。这是对历史的陈述，不是给新代码用的常量。
LEGACY_DIM = 1024
UNKNOWN_MODEL = "unknown"
ACTIVE_SPACE_KEY = "embedding_active_space"

_TABLES = (
    ("paper_vectors", "paper_id", "papers"),
    ("paper_chunk_vectors", "chunk_id", "paper_chunks"),
    ("idea_vectors", "idea_id", "ideas"),
)

_settings = sa.table(
    "system_settings",
    sa.column("key", sa.String),
    sa.column("value", sa.JSON().with_variant(JSONB(), "postgresql")),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


def _not_null(column: str, is_postgres: bool) -> str:
    """「这一行真有向量」的判据。

    sqlite 上 embedding 是 JSON 列，SQLAlchemy 把 Python None 存成字面量 ``null``，
    只判 IS NOT NULL 会把没建向量的行也捞进来（同 services/lab.py::_has_vector）。
    """
    if is_postgres:
        return f"{column} IS NOT NULL"
    return f"{column} IS NOT NULL AND CAST({column} AS TEXT) <> 'null'"


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"
    # postgres 用不带维度的 pgvector 列；其他方言回退 JSON（只存不查）
    vector_type = Vector() if is_postgres else sa.JSON()

    for table, owner_column, owner_table in _TABLES:
        op.create_table(
            table,
            sa.Column(
                owner_column,
                sa.Uuid(),
                sa.ForeignKey(f"{owner_table}.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("space", sa.String(length=160), primary_key=True),
            sa.Column("dim", sa.Integer(), nullable=False),
            sa.Column("embedding", vector_type, nullable=False),
            sa.Column("model", sa.String(length=128), nullable=False),
            sa.Column("text_version", sa.SmallInteger(), nullable=False, server_default="1"),
            sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index(f"ix_{table}_space", table, ["space"])

    _migrate_existing_vectors(bind, is_postgres)


def _migrate_existing_vectors(bind: sa.Connection, is_postgres: bool) -> None:
    now = "now()" if is_postgres else "CURRENT_TIMESTAMP"
    # 存量行的 embedding_model 为 NULL 时的兜底：全局路由表里配的嵌入模型
    fallback = (
        bind.execute(
            sa.text(
                "SELECT model FROM model_routes "
                "WHERE stage = 'embedding' AND owner_id IS NULL LIMIT 1"
            )
        ).scalar()
        or UNKNOWN_MODEL
    )
    # suffix 与 dim 必须是两个参数：同一个参数既参与字符串拼接又要塞进 integer 列时，
    # postgres 会按第一次出现把它推断成 text，之后插 integer 列直接 DatatypeMismatch。
    # （sqlite 类型宽松，不会暴露这个问题。）
    params = {"fallback": fallback, "dim": LEGACY_DIM, "suffix": f"@{LEGACY_DIM}"}

    bind.execute(
        sa.text(
            "INSERT INTO paper_vectors "
            "(paper_id, space, dim, embedding, model, text_version, built_at) "
            "SELECT p.id, COALESCE(p.embedding_model, :fallback) || :suffix, :dim, "
            "p.embedding, COALESCE(p.embedding_model, :fallback), 1, "
            f"COALESCE(p.embedding_at, {now}) "
            "FROM papers p "
            f"WHERE {_not_null('p.embedding', is_postgres)}"
        ),
        params,
    )
    bind.execute(
        sa.text(
            "INSERT INTO paper_chunk_vectors "
            "(chunk_id, space, dim, embedding, model, text_version, built_at) "
            "SELECT c.id, "
            "COALESCE(p.chunk_embedding_model, p.embedding_model, :fallback) || :suffix, "
            ":dim, c.embedding, "
            "COALESCE(p.chunk_embedding_model, p.embedding_model, :fallback), 1, "
            f"COALESCE(p.chunk_embedding_at, p.embedding_at, {now}) "
            "FROM paper_chunks c JOIN papers p ON p.id = c.paper_id "
            f"WHERE {_not_null('c.embedding', is_postgres)}"
        ),
        params,
    )

    active = _dominant_space(bind)
    space = active or f"{fallback}@{LEGACY_DIM}"
    model, _, dim = space.rpartition("@")
    bind.execute(
        sa.text(
            "INSERT INTO idea_vectors "
            "(idea_id, space, dim, embedding, model, text_version, built_at) "
            "SELECT i.id, :space, :dim, i.embedding, :model, 1, "
            f"COALESCE(i.updated_at, {now}) "
            "FROM ideas i "
            f"WHERE {_not_null('i.embedding', is_postgres)}"
        ),
        {"space": space, "model": model, "dim": int(dim)},
    )

    # 只有真搬到了向量才写激活空间：空库留给首次嵌入按模型实际维度自行初始化
    moved = sum(
        bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0
        for table, _, _ in _TABLES
    )
    if moved:
        stamp = datetime.now(UTC)
        bind.execute(
            _settings.insert().values(
                key=ACTIVE_SPACE_KEY,
                value={"model": model, "dim": int(dim)},
                created_at=stamp,
                updated_at=stamp,
            )
        )


def _dominant_space(bind: sa.Connection) -> str | None:
    """搬迁后行数最多的空间；一行向量都没有返回 None。

    存量本来就混装了多个模型的向量时（用户曾自管嵌入模型），这里不做归并——
    少数派留在库里当作另一个空间，等重建任务补齐，而不是假装它们本来就可比。
    """
    row = bind.execute(
        sa.text(
            "SELECT space, SUM(n) AS total FROM ("
            "  SELECT space, COUNT(*) AS n FROM paper_vectors GROUP BY space"
            "  UNION ALL"
            "  SELECT space, COUNT(*) AS n FROM paper_chunk_vectors GROUP BY space"
            ") s GROUP BY space ORDER BY total DESC, space LIMIT 1"
        )
    ).first()
    return row[0] if row is not None else None


def downgrade() -> None:
    op.execute(f"DELETE FROM system_settings WHERE key = '{ACTIVE_SPACE_KEY}'")
    op.execute("DELETE FROM system_settings WHERE key = 'embedding_building_space'")
    for table, _, _ in _TABLES:
        op.drop_index(f"ix_{table}_space", table_name=table)
        op.drop_table(table)
