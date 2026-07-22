"""方向文献库 + 论文内容池（P4 迁移 A）

新建 direction_libraries / direction_library_curators / library_papers 三张表；
papers 加全局去重键 dedup_key；为每个现有 project 生成 1:1 隐式方向库并把论文
归属 + 判断字段复制为成员行；回填 dedup_key 并按「chunks 最多 / 字段最全」
消解同键冲突后加唯一索引。project_id 冗余列在迁移 B（P4 收尾）删除，此处先放开
NOT NULL 以便新代码不再写入。

Revision ID: f7c2abfe8aeb
Revises: 94e6bc81c510
Create Date: 2026-07-22
"""

import hashlib
import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f7c2abfe8aeb"
down_revision: str | None = "94e6bc81c510"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSON = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


# ---- 与 app/services/dedup.py 同口径（迁移内自带副本，避免依赖应用代码演化）----


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _normalize_author(name: str) -> str:
    return re.sub(r"[^a-z一-鿿0-9]+", " ", name.lower()).strip()


def _pool_dedup_key(
    arxiv_id: str | None,
    doi: str | None,
    title: str,
    year: int | None,
    authors: list[Any] | None,
) -> str:
    if arxiv_id:
        return f"arxiv:{arxiv_id.lower()}"
    if doi:
        return f"doi:{doi.lower()}"
    parts = [_normalize_title(title)]
    if year is not None:
        parts.append(str(year))
    first = authors[0] if authors else None
    name = first.get("name") if isinstance(first, dict) else first
    if isinstance(name, str) and name.strip():
        parts.append(_normalize_author(name))
    return f"title:{hashlib.sha1('|'.join(parts).encode()).hexdigest()}"


def _table(name: str, *cols: sa.Column) -> sa.TableClause:
    return sa.table(name, *cols)


def upgrade() -> None:
    bind = op.get_bind()

    # ---- 1. 新表 ----
    op.create_table(
        "direction_libraries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("statement", sa.Text(), nullable=True),
        sa.Column("rubric", _JSON, nullable=True),
        sa.Column("anchors", _JSON, nullable=True),
        sa.Column("ingest_state", _JSON, nullable=True),
        sa.Column("cadence", sa.String(32), nullable=True),
        sa.Column("monthly_budget", sa.Integer(), nullable=True),
        sa.Column(
            "created_by", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id"),
    )
    op.create_table(
        "direction_library_curators",
        sa.Column(
            "library_id",
            sa.Uuid(),
            sa.ForeignKey("direction_libraries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "library_papers",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "library_id",
            sa.Uuid(),
            sa.ForeignKey("direction_libraries.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "paper_id", sa.Uuid(), sa.ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("relevance_score", sa.Float(), nullable=True),
        sa.Column("tldr_note", sa.Text(), nullable=True),
        sa.Column("wiki_content", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("trash_reason", sa.String(16), nullable=True),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("compiled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("compiled_model", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("library_id", "paper_id", name="uq_library_papers_library_paper"),
    )
    op.create_index("ix_library_papers_library_id", "library_papers", ["library_id"])
    op.create_index("ix_library_papers_paper_id", "library_papers", ["paper_id"])
    op.create_index("ix_library_papers_library_status", "library_papers", ["library_id", "status"])

    # ---- 2. papers.dedup_key + 放开 project_id 冗余列的 NOT NULL（迁移 B 删列）----
    op.add_column("papers", sa.Column("dedup_key", sa.String(512), nullable=True))
    with op.batch_alter_table("papers") as batch:
        batch.alter_column("project_id", existing_type=sa.Uuid(), nullable=True)
    with op.batch_alter_table("paper_chunks") as batch:
        batch.alter_column("project_id", existing_type=sa.Uuid(), nullable=True)

    # ---- 3. 数据迁移：隐式库 + 成员行 + dedup_key 回填 + 同键冲突消解 ----
    now = datetime.now(UTC)
    projects_t = _table(
        "projects",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("definition", _JSON),
        sa.column("ingest_state", _JSON),
        sa.column("owner_id", sa.Uuid()),
    )
    libs_t = _table(
        "direction_libraries",
        sa.column("id", sa.Uuid()),
        sa.column("name", sa.String()),
        sa.column("statement", sa.Text()),
        sa.column("rubric", _JSON),
        sa.column("anchors", _JSON),
        sa.column("ingest_state", _JSON),
        sa.column("cadence", sa.String()),
        sa.column("created_by", sa.Uuid()),
        sa.column("project_id", sa.Uuid()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    papers_t = _table(
        "papers",
        sa.column("id", sa.Uuid()),
        sa.column("project_id", sa.Uuid()),
        sa.column("arxiv_id", sa.String()),
        sa.column("doi", sa.String()),
        sa.column("title", sa.Text()),
        sa.column("year", sa.Integer()),
        sa.column("authors", _JSON),
        sa.column("abstract", sa.Text()),
        sa.column("pdf_path", sa.String()),
        sa.column("full_text_path", sa.String()),
        sa.column("relevance_score", sa.Float()),
        sa.column("wiki_content", sa.Text()),
        sa.column("status", sa.String()),
        sa.column("trash_reason", sa.String()),
        sa.column("scored_at", sa.DateTime(timezone=True)),
        sa.column("compiled_at", sa.DateTime(timezone=True)),
        sa.column("compiled_model", sa.String()),
        sa.column("dedup_key", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    members_t = _table(
        "library_papers",
        sa.column("id", sa.Uuid()),
        sa.column("library_id", sa.Uuid()),
        sa.column("paper_id", sa.Uuid()),
        sa.column("relevance_score", sa.Float()),
        sa.column("wiki_content", sa.Text()),
        sa.column("status", sa.String()),
        sa.column("trash_reason", sa.String()),
        sa.column("scored_at", sa.DateTime(timezone=True)),
        sa.column("compiled_at", sa.DateTime(timezone=True)),
        sa.column("compiled_model", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    lib_by_project: dict[Any, uuid.UUID] = {}
    for row in bind.execute(sa.select(projects_t)).mappings():
        definition = row["definition"] if isinstance(row["definition"], dict) else {}
        lib_id = uuid.uuid4()
        lib_by_project[row["id"]] = lib_id
        bind.execute(
            libs_t.insert().values(
                id=lib_id,
                name=row["name"],
                statement=definition.get("statement"),
                rubric=definition.get("rubric"),
                anchors=definition.get("anchor_papers"),
                ingest_state=row["ingest_state"],
                cadence=definition.get("cadence"),
                created_by=row["owner_id"],
                project_id=row["id"],
                created_at=now,
                updated_at=now,
            )
        )

    paper_rows = list(bind.execute(sa.select(papers_t)).mappings())
    for row in paper_rows:
        if row["project_id"] in lib_by_project:
            bind.execute(
                members_t.insert().values(
                    id=uuid.uuid4(),
                    library_id=lib_by_project[row["project_id"]],
                    paper_id=row["id"],
                    relevance_score=row["relevance_score"],
                    wiki_content=row["wiki_content"],
                    status=row["status"] or "candidate",
                    trash_reason=row["trash_reason"],
                    scored_at=row["scored_at"],
                    compiled_at=row["compiled_at"],
                    compiled_model=row["compiled_model"],
                    created_at=row["created_at"] or now,
                    updated_at=row["updated_at"] or now,
                )
            )
        key = _pool_dedup_key(
            row["arxiv_id"], row["doi"], row["title"], row["year"], row["authors"]
        )
        bind.execute(
            papers_t.update().where(papers_t.c.id == row["id"]).values(dedup_key=key)
        )

    _resolve_dedup_conflicts(bind, paper_rows)

    op.create_index("ix_papers_dedup_key", "papers", ["dedup_key"], unique=True)


def _resolve_dedup_conflicts(bind: sa.engine.Connection, paper_rows: list[Any]) -> None:
    """同 dedup_key 多行：保留 chunks 最多 / 字段最全的一行，子表 repoint 后删除其余。"""
    chunk_counts: dict[Any, int] = {
        pid: int(n)
        for pid, n in bind.execute(
            sa.text("SELECT paper_id, COUNT(*) FROM paper_chunks GROUP BY paper_id")
        )
    }
    groups: dict[str, list[Any]] = {}
    for row in paper_rows:
        key = _pool_dedup_key(
            row["arxiv_id"], row["doi"], row["title"], row["year"], row["authors"]
        )
        groups.setdefault(key, []).append(row)

    for rows in groups.values():
        if len(rows) < 2:
            continue

        def completeness(row: Any) -> tuple:
            return (
                chunk_counts.get(row["id"], 0),
                sum(
                    1
                    for f in ("full_text_path", "pdf_path", "wiki_content", "abstract", "doi")
                    if row[f]
                ),
                # 平手时保留最早入库的一行（id 兜底保证确定性）
                -(row["created_at"].timestamp() if row["created_at"] else 0),
                str(row["id"]),
            )

        rows = sorted(rows, key=completeness, reverse=True)
        survivor = rows[0]["id"]
        for loser_row in rows[1:]:
            _repoint_paper_refs(bind, loser=loser_row["id"], survivor=survivor)


def _repoint_paper_refs(bind: sa.engine.Connection, *, loser: Any, survivor: Any) -> None:
    params = {"loser": loser, "survivor": survivor}
    # 成员行 / 概念关联 / 标签关联 / 个人状态：survivor 侧已存在同键行时删掉 loser 行
    for sql_exists, sql_update, sql_delete in (
        (
            "SELECT library_id FROM library_papers WHERE paper_id = :survivor",
            "UPDATE library_papers SET paper_id = :survivor "
            "WHERE paper_id = :loser AND library_id NOT IN ({sub})",
            "DELETE FROM library_papers WHERE paper_id = :loser",
        ),
        (
            "SELECT concept_id FROM paper_concepts WHERE paper_id = :survivor",
            "UPDATE paper_concepts SET paper_id = :survivor "
            "WHERE paper_id = :loser AND concept_id NOT IN ({sub})",
            "DELETE FROM paper_concepts WHERE paper_id = :loser",
        ),
        (
            "SELECT tag_id FROM paper_tag_links WHERE paper_id = :survivor",
            "UPDATE paper_tag_links SET paper_id = :survivor "
            "WHERE paper_id = :loser AND tag_id NOT IN ({sub})",
            "DELETE FROM paper_tag_links WHERE paper_id = :loser",
        ),
        (
            "SELECT user_id FROM paper_user_meta WHERE paper_id = :survivor",
            "UPDATE paper_user_meta SET paper_id = :survivor "
            "WHERE paper_id = :loser AND user_id NOT IN ({sub})",
            "DELETE FROM paper_user_meta WHERE paper_id = :loser",
        ),
    ):
        bind.execute(sa.text(sql_update.format(sub=sql_exists)), params)
        bind.execute(sa.text(sql_delete), params)
    # 无唯一约束的子表 / 软引用：整体 repoint
    for sql in (
        "UPDATE paper_notes SET paper_id = :survivor WHERE paper_id = :loser",
        "UPDATE paper_highlights SET paper_id = :survivor WHERE paper_id = :loser",
        "UPDATE user_library_entries SET last_paper_id = :survivor WHERE last_paper_id = :loser",
        "UPDATE user_publications SET paper_id = :survivor WHERE paper_id = :loser",
    ):
        bind.execute(sa.text(sql), params)
    # loser 的 chunks 不并入（survivor 已是 chunks 最多的一行），随论文删除
    bind.execute(sa.text("DELETE FROM paper_chunks WHERE paper_id = :loser"), params)
    bind.execute(sa.text("DELETE FROM papers WHERE id = :loser"), params)


def downgrade() -> None:
    op.drop_index("ix_papers_dedup_key", table_name="papers")
    with op.batch_alter_table("paper_chunks") as batch:
        batch.alter_column("project_id", existing_type=sa.Uuid(), nullable=False)
    with op.batch_alter_table("papers") as batch:
        batch.alter_column("project_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("papers", "dedup_key")
    op.drop_index("ix_library_papers_library_status", table_name="library_papers")
    op.drop_index("ix_library_papers_paper_id", table_name="library_papers")
    op.drop_index("ix_library_papers_library_id", table_name="library_papers")
    op.drop_table("library_papers")
    op.drop_table("direction_library_curators")
    op.drop_table("direction_libraries")
