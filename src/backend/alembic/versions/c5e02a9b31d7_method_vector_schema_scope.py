"""method vectors are scoped to the card they came from (#772)

Revision ID: c5e02a9b31d7
Revises: b4d91f7a2c08
Create Date: 2026-09-14

学科包出现后，同一篇论文可能有多张方法卡（内置的 + 某学科的），各自是**对这篇论文
的一种读法**。向量此前只按 (paper, axis, space) 存，于是两个声明了不同学科的库刷同
一篇论文会互相覆盖：界面上显示本库那张卡，算分却用了另一个领域的文本。

按 schema_id 分开存，只在读法真的不同时才多一份向量。加 library_id 才是错的——
那会让用同一张卡的两个库各存一份完全相同的向量。

回填 "method"：存量行全部来自内置卡（学科卡的索引刷新是 #771 之后才有的事），
所以存量数据的检索结果一字不变。**不重建表丢数据**：向量虽是派生物，但只有富集
时才会重算，存量论文一旦丢失就永远不会被补回来，方法检索会对它们永久失灵。
"""

import sqlalchemy as sa

from alembic import op

revision = "c5e02a9b31d7"
down_revision = "b4d91f7a2c08"
branch_labels = None
depends_on = None

_OLD_PK = ["paper_id", "axis", "space"]
_NEW_PK = ["paper_id", "axis", "space", "schema_id"]

#: sqlite 反射出来的主键是**无名的**，按 "method_vectors_pkey" 去 drop 会报
#: "No such constraint"。给 batch 一个命名约定，反射出的主键才有名字可引用。
#: postgres 那边主键真叫 method_vectors_pkey，直接 ALTER 即可，两条路分开走。
_SQLITE_NAMING = {"pk": "pk_%(table_name)s"}


def _repoint_primary_key(columns: list[str]) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "method_vectors", naming_convention=_SQLITE_NAMING
        ) as batch:
            batch.drop_constraint("pk_method_vectors", type_="primary")
            batch.create_primary_key("pk_method_vectors", columns)
    else:
        op.drop_constraint("method_vectors_pkey", "method_vectors", type_="primary")
        op.create_primary_key("method_vectors_pkey", "method_vectors", columns)


def upgrade() -> None:
    # 先可空加列再回填再收紧：存量表有行时直接 NOT NULL 会失败
    op.add_column("method_vectors", sa.Column("schema_id", sa.String(64), nullable=True))
    op.execute(sa.text("UPDATE method_vectors SET schema_id = 'method'"))
    with op.batch_alter_table("method_vectors") as batch:
        batch.alter_column(
            "schema_id",
            existing_type=sa.String(64),
            nullable=False,
            server_default="method",
        )
    _repoint_primary_key(_NEW_PK)


def downgrade() -> None:
    # 回退前先把非内置卡的向量删掉：主键收窄后它们会与内置行撞键，
    # 而「保留哪一行」没有正确答案——学科卡的向量本就是这次改动才有的
    op.execute(sa.text("DELETE FROM method_vectors WHERE schema_id <> 'method'"))
    _repoint_primary_key(_OLD_PK)
    with op.batch_alter_table("method_vectors") as batch:
        batch.drop_column("schema_id")
