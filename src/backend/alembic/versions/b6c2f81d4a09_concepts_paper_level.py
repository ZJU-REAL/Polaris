"""概念统一到论文级：concepts 去 library_id、slug 全局唯一、同名合并

概念本来按库分版本（concepts.library_id NOT NULL），同一个词在每个库各存一份、
定义还各不相同；解读统一成论文级之后更没有归属可言（每日推送的论文不属于任何库）。
本迁移：
① 同名概念合并成一份——保留**定义最长**的那行（AI 写的定义，长的通常把边界和用途
   都写全了；最新的可能只是碰巧后编译），其余行的 paper_concepts 关联改指到它（同一
   篇论文同时挂着两份同名概念时去重丢弃）；
② 剩下的行里若还有 slug 相撞（不同名字折叠出同一个 slug），给后来者加随机后缀；
③ concepts 去掉 library_id、唯一键从 (library_id, slug) 改成 slug 全局唯一。

「某个库有哪些概念」不再存，一律推导：库的论文（library_papers）→ 关联概念
（paper_concepts）→ 去重，故不建 library_concepts 关联表。

**能不删就不删**：被合并掉的整行（连同原 library_id、定义）与被改指/丢弃的关联，
分别留档在 concepts_pre_unify / paper_concepts_pre_unify；downgrade 据此完整还原
（重建 library_id 列、复活合并掉的行与关联、撤销改指），还原后再删两张留档表。

Revision ID: b6c2f81d4a09
Revises: a7d0c9e51b34
Create Date: 2026-07-25
"""

import uuid
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "b6c2f81d4a09"
down_revision: str | None = "a7d0c9e51b34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tables() -> dict[str, sa.Table]:
    meta = sa.MetaData()
    concepts = sa.Table(
        "concepts",
        meta,
        sa.Column("id", sa.Uuid()),
        sa.Column("library_id", sa.Uuid()),
        sa.Column("name", sa.String(length=255)),
        sa.Column("slug", sa.String(length=255)),
        sa.Column("definition", sa.Text()),
        sa.Column("category", sa.String(length=64)),
        sa.Column("wiki_content", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    paper_concepts = sa.Table(
        "paper_concepts",
        meta,
        sa.Column("paper_id", sa.Uuid()),
        sa.Column("concept_id", sa.Uuid()),
    )
    backup = sa.Table(
        "concepts_pre_unify",
        meta,
        sa.Column("id", sa.Uuid()),
        sa.Column("library_id", sa.Uuid()),
        sa.Column("merged_into", sa.Uuid()),
        sa.Column("name", sa.String(length=255)),
        sa.Column("slug", sa.String(length=255)),
        sa.Column("definition", sa.Text()),
        sa.Column("category", sa.String(length=64)),
        sa.Column("wiki_content", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    link_backup = sa.Table(
        "paper_concepts_pre_unify",
        meta,
        sa.Column("paper_id", sa.Uuid()),
        sa.Column("concept_id", sa.Uuid()),
        sa.Column("merged_into", sa.Uuid()),
        sa.Column("repointed", sa.Boolean()),
    )
    return {
        "concepts": concepts,
        "links": paper_concepts,
        "backup": backup,
        "link_backup": link_backup,
    }


def _merge_duplicates(conn: sa.Connection, t: dict[str, sa.Table]) -> None:
    """同名概念合并成一份（保留定义最长的），关联改指 + 留档。"""
    rows = conn.execute(
        sa.select(
            t["concepts"].c.id,
            t["concepts"].c.library_id,
            t["concepts"].c.name,
            t["concepts"].c.slug,
            t["concepts"].c.definition,
            t["concepts"].c.category,
            t["concepts"].c.wiki_content,
            t["concepts"].c.created_at,
            t["concepts"].c.updated_at,
        )
    ).mappings().all()
    if not rows:
        return

    # 全量留档（含没被合并的行：downgrade 要按它还原 library_id / slug）
    by_name: dict[str, list[Any]] = {}
    for row in rows:
        by_name.setdefault(row["name"], []).append(row)

    keeper_of: dict[uuid.UUID, uuid.UUID] = {}  # 被合并行 → 保留行
    for group in by_name.values():
        if len(group) == 1:
            continue
        # 定义最长者优先；并列时按 created_at 早者（稳定，不看编译先后）
        keeper = sorted(
            group, key=lambda r: (-len(r["definition"] or ""), r["created_at"] or 0, str(r["id"]))
        )[0]
        for row in group:
            if row["id"] != keeper["id"]:
                keeper_of[row["id"]] = keeper["id"]

    conn.execute(
        t["backup"].insert(),
        [
            {
                "id": row["id"],
                "library_id": row["library_id"],
                "merged_into": keeper_of.get(row["id"]),
                "name": row["name"],
                "slug": row["slug"],
                "definition": row["definition"],
                "category": row["category"],
                "wiki_content": row["wiki_content"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ],
    )
    if not keeper_of:
        return

    # 关联改指：目标已有同一 (paper, keeper) 时丢弃（去重），否则改指
    links = conn.execute(
        sa.select(t["links"].c.paper_id, t["links"].c.concept_id)
    ).all()
    existing = {(paper_id, concept_id) for paper_id, concept_id in links}
    moved: list[dict[str, Any]] = []
    for paper_id, concept_id in links:
        keeper = keeper_of.get(concept_id)
        if keeper is None:
            continue
        repointed = (paper_id, keeper) not in existing
        moved.append(
            {
                "paper_id": paper_id,
                "concept_id": concept_id,
                "merged_into": keeper,
                "repointed": repointed,
            }
        )
        if repointed:
            existing.add((paper_id, keeper))
            conn.execute(
                t["links"]
                .update()
                .where(
                    t["links"].c.paper_id == paper_id,
                    t["links"].c.concept_id == concept_id,
                )
                .values(concept_id=keeper)
            )
        else:
            conn.execute(
                t["links"]
                .delete()
                .where(
                    t["links"].c.paper_id == paper_id,
                    t["links"].c.concept_id == concept_id,
                )
            )
    if moved:
        conn.execute(t["link_backup"].insert(), moved)

    conn.execute(t["concepts"].delete().where(t["concepts"].c.id.in_(list(keeper_of))))


def _dedupe_slugs(conn: sa.Connection, t: dict[str, sa.Table]) -> None:
    """合并后仍相撞的 slug（不同名字折叠成同一个）加随机后缀，保证全局唯一。"""
    rows = conn.execute(
        sa.select(t["concepts"].c.id, t["concepts"].c.slug).order_by(t["concepts"].c.created_at)
    ).all()
    seen: set[str] = set()
    for concept_id, slug in rows:
        if slug not in seen:
            seen.add(slug)
            continue
        new_slug = f"{slug}-{uuid.uuid4().hex[:6]}"
        while new_slug in seen:
            new_slug = f"{slug}-{uuid.uuid4().hex[:6]}"
        seen.add(new_slug)
        conn.execute(
            t["concepts"].update().where(t["concepts"].c.id == concept_id).values(slug=new_slug)
        )


def upgrade() -> None:
    op.create_table(
        "concepts_pre_unify",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("library_id", sa.Uuid(), nullable=True),
        sa.Column("merged_into", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("definition", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column("wiki_content", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "paper_concepts_pre_unify",
        sa.Column("paper_id", sa.Uuid(), nullable=False),
        sa.Column("concept_id", sa.Uuid(), nullable=False),
        sa.Column("merged_into", sa.Uuid(), nullable=False),
        sa.Column("repointed", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("paper_id", "concept_id"),
    )

    t = _tables()
    conn = op.get_bind()
    _merge_duplicates(conn, t)
    _dedupe_slugs(conn, t)

    op.drop_index("ix_concepts_library_id", table_name="concepts")
    with op.batch_alter_table("concepts") as batch:
        batch.drop_constraint("uq_concepts_library_slug", type_="unique")
        batch.drop_constraint("fk_concepts_library_id", type_="foreignkey")
        batch.drop_column("library_id")
        batch.create_unique_constraint("uq_concepts_slug", ["slug"])


def downgrade() -> None:
    with op.batch_alter_table("concepts") as batch:
        batch.drop_constraint("uq_concepts_slug", type_="unique")
        batch.add_column(sa.Column("library_id", sa.Uuid(), nullable=True))

    t = _tables()
    conn = op.get_bind()
    backup = conn.execute(
        sa.select(
            t["backup"].c.id,
            t["backup"].c.library_id,
            t["backup"].c.merged_into,
            t["backup"].c.name,
            t["backup"].c.slug,
            t["backup"].c.definition,
            t["backup"].c.category,
            t["backup"].c.wiki_content,
            t["backup"].c.created_at,
            t["backup"].c.updated_at,
        )
    ).mappings().all()

    # ① 复活被合并掉的行；② 存活行的 library_id / slug 按留档还原
    revived = [
        {
            "id": row["id"],
            "library_id": row["library_id"],
            "name": row["name"],
            "slug": row["slug"],
            "definition": row["definition"],
            "category": row["category"],
            "wiki_content": row["wiki_content"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in backup
        if row["merged_into"] is not None
    ]
    if revived:
        conn.execute(t["concepts"].insert(), revived)
    for row in backup:
        if row["merged_into"] is None:
            conn.execute(
                t["concepts"]
                .update()
                .where(t["concepts"].c.id == row["id"])
                .values(library_id=row["library_id"], slug=row["slug"])
            )

    # ③ 关联还原：改指的撤回（删保留者那条，插回原概念），去重丢弃的直接插回
    moved = conn.execute(
        sa.select(
            t["link_backup"].c.paper_id,
            t["link_backup"].c.concept_id,
            t["link_backup"].c.merged_into,
            t["link_backup"].c.repointed,
        )
    ).all()
    for paper_id, concept_id, merged_into, repointed in moved:
        if repointed:
            conn.execute(
                t["links"]
                .delete()
                .where(
                    t["links"].c.paper_id == paper_id,
                    t["links"].c.concept_id == merged_into,
                )
            )
        conn.execute(t["links"].insert().values(paper_id=paper_id, concept_id=concept_id))

    # 存量库都有值后再收紧成 NOT NULL + 恢复原唯一键/索引
    conn.execute(t["concepts"].delete().where(t["concepts"].c.library_id.is_(None)))
    with op.batch_alter_table("concepts") as batch:
        batch.alter_column("library_id", existing_type=sa.Uuid(), nullable=False)
        batch.create_foreign_key(
            "fk_concepts_library_id",
            "direction_libraries",
            ["library_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint("uq_concepts_library_slug", ["library_id", "slug"])
    op.create_index("ix_concepts_library_id", "concepts", ["library_id"])

    op.drop_table("paper_concepts_pre_unify")
    op.drop_table("concepts_pre_unify")
