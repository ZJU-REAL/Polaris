"""重复论文合并（P6 策展工具，docs-dev/rfc-paper-content-pool.md §5）。

模糊去重的漏网（预印本 vs 正式版、标题变体）由策展人人工确认合并：把 drop 行的
全部归属 repoint 到 keep 行后删除 drop。所有归属只经 paper_id 引用，合并是局部操作。

约定：
- 冲突表（同库 / 同课题 / 同用户已各有一行）按「keep 行优先、缺项用 drop 补」合并；
- keep 行缺全文 / 图 / 元数据时从 drop 行搬运（chunks 仅在 keep 无分段时整体迁移）；
- drop 的 dedup_key 无法与 keep 并存（UNIQUE），随行删除并写进返回报告；
- 全程一个事务，最后统一 commit；paper 不存在或 keep==drop 抛 ValueError。
"""

import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library import UserLibraryEntry
from app.models.library_direction import LibraryPaper
from app.models.paper import (
    Paper,
    PaperChunk,
    PaperHighlight,
    PaperNote,
    PaperUserMeta,
    paper_concepts,
    paper_tag_links,
)
from app.models.publication import UserPublication
from app.models.topic_shelf import TopicPaper

# 内容池行上「keep 缺则用 drop 补」的字段（判断性字段在成员行，另行处理）
_FILLABLE_PAPER_FIELDS = (
    "arxiv_id",
    "doi",
    "abstract",
    "year",
    "venue",
    "url",
    "published_at",
    "pdf_path",
    "full_text_path",
    "tldr",
    "figures",
    "embedding",
    "affiliations",
    "authors",
)

# 成员行上的判断字段：同库冲突时 keep 缺则补
_FILLABLE_MEMBERSHIP_FIELDS = (
    "relevance_score",
    "tldr_note",
    "wiki_content",
    "scored_at",
    "compiled_at",
    "compiled_model",
)

# 阅读状态推进序（冲突时取两边更靠后的）
_READING_ORDER = {"unread": 0, "reading": 1, "read": 2}
# 成员行状态推进序（excluded/included 人工态不参与自动升级）
_STATUS_ORDER = {"candidate": 0, "scored": 1, "fetched": 2, "compiled": 3}


async def _repoint_associations(
    session: AsyncSession, table: Any, col: str, *, keep_id: uuid.UUID, drop_id: uuid.UUID
) -> tuple[int, int]:
    """纯关联表（paper_concepts / paper_tag_links）：两边都有的删 drop 侧，其余 repoint。

    返回 (repointed, deduped)。
    """
    paper_col = table.c.paper_id
    other_col = table.c[col]
    dup_subq = select(other_col).where(paper_col == keep_id)
    deduped = (
        await session.execute(
            delete(table).where(paper_col == drop_id, other_col.in_(dup_subq))
        )
    ).rowcount
    repointed = (
        await session.execute(
            update(table).where(paper_col == drop_id).values(paper_id=keep_id)
        )
    ).rowcount
    return int(repointed or 0), int(deduped or 0)


async def merge_papers(
    session: AsyncSession, *, keep_id: uuid.UUID, drop_id: uuid.UUID
) -> dict[str, Any]:
    """把 drop 论文的所有归属并入 keep 后删除 drop 行，返回合并报告。"""
    if keep_id == drop_id:
        raise ValueError("keep and drop must be different papers")
    keep = await session.get(Paper, keep_id)
    drop = await session.get(Paper, drop_id)
    if keep is None or drop is None:
        raise ValueError("paper not found")

    report: dict[str, Any] = {
        "kept_id": str(keep_id),
        "dropped_id": str(drop_id),
        "dropped_dedup_key": drop.dedup_key,
    }

    # ---- 1. library_papers：同库冲突合并判断字段，否则 repoint ----
    keep_members = {
        m.library_id: m
        for m in (
            await session.execute(select(LibraryPaper).where(LibraryPaper.paper_id == keep_id))
        ).scalars()
    }
    drop_members = list(
        (
            await session.execute(select(LibraryPaper).where(LibraryPaper.paper_id == drop_id))
        ).scalars()
    )
    lib_repointed = lib_merged = 0
    for membership in drop_members:
        existing = keep_members.get(membership.library_id)
        if existing is None:
            membership.paper_id = keep_id
            lib_repointed += 1
            continue
        for field in _FILLABLE_MEMBERSHIP_FIELDS:
            if getattr(existing, field) is None and getattr(membership, field) is not None:
                setattr(existing, field, getattr(membership, field))
        # drop 侧流程走得更远（如已编译）且 keep 仍在自动流程中 → 采纳 drop 的状态
        if (
            existing.status in _STATUS_ORDER
            and membership.status in _STATUS_ORDER
            and _STATUS_ORDER[membership.status] > _STATUS_ORDER[existing.status]
        ):
            existing.status = membership.status
        await session.delete(membership)
        lib_merged += 1
    report["library_memberships"] = {"repointed": lib_repointed, "merged": lib_merged}

    # ---- 2. topic_papers：同课题冲突保留 keep 行（缺快照/备注则补），否则 repoint ----
    keep_shelf = {
        t.topic_id: t
        for t in (
            await session.execute(select(TopicPaper).where(TopicPaper.paper_id == keep_id))
        ).scalars()
    }
    shelf_repointed = shelf_merged = 0
    for row in (
        await session.execute(select(TopicPaper).where(TopicPaper.paper_id == drop_id))
    ).scalars():
        existing = keep_shelf.get(row.topic_id)
        if existing is None:
            row.paper_id = keep_id
            shelf_repointed += 1
            continue
        if existing.wiki_snapshot is None and row.wiki_snapshot is not None:
            existing.wiki_snapshot = row.wiki_snapshot
            existing.snapshot_at = row.snapshot_at
        if not existing.note and row.note:
            existing.note = row.note
        await session.delete(row)
        shelf_merged += 1
    report["topic_papers"] = {"repointed": shelf_repointed, "merged": shelf_merged}

    # ---- 3. paper_user_meta：同用户冲突合并（starred 取并、阅读状态取更靠后的） ----
    keep_meta = {
        m.user_id: m
        for m in (
            await session.execute(select(PaperUserMeta).where(PaperUserMeta.paper_id == keep_id))
        ).scalars()
    }
    meta_repointed = meta_merged = 0
    for meta in (
        await session.execute(select(PaperUserMeta).where(PaperUserMeta.paper_id == drop_id))
    ).scalars():
        existing = keep_meta.get(meta.user_id)
        if existing is None:
            meta.paper_id = keep_id
            meta_repointed += 1
            continue
        existing.starred = existing.starred or meta.starred
        if _READING_ORDER.get(meta.reading_status, 0) > _READING_ORDER.get(
            existing.reading_status, 0
        ):
            existing.reading_status = meta.reading_status
        await session.delete(meta)
        meta_merged += 1
    report["paper_user_meta"] = {"repointed": meta_repointed, "merged": meta_merged}

    # ---- 4. 笔记 / 划线：无唯一约束，整体 repoint ----
    notes = (
        await session.execute(
            update(PaperNote).where(PaperNote.paper_id == drop_id).values(paper_id=keep_id)
        )
    ).rowcount
    highlights = (
        await session.execute(
            update(PaperHighlight)
            .where(PaperHighlight.paper_id == drop_id)
            .values(paper_id=keep_id)
        )
    ).rowcount
    report["notes_repointed"] = int(notes or 0)
    report["highlights_repointed"] = int(highlights or 0)

    # ---- 5. 概念链 / 标签链：两边都有的去重，其余 repoint ----
    repointed, deduped = await _repoint_associations(
        session, paper_concepts, "concept_id", keep_id=keep_id, drop_id=drop_id
    )
    report["concept_links"] = {"repointed": repointed, "deduped": deduped}
    repointed, deduped = await _repoint_associations(
        session, paper_tag_links, "tag_id", keep_id=keep_id, drop_id=drop_id
    )
    report["tag_links"] = {"repointed": repointed, "deduped": deduped}

    # ---- 6. 软引用：个人库回跳 / 发表记录 ----
    entries = (
        await session.execute(
            update(UserLibraryEntry)
            .where(UserLibraryEntry.last_paper_id == drop_id)
            .values(last_paper_id=keep_id)
        )
    ).rowcount
    publications = (
        await session.execute(
            update(UserPublication)
            .where(UserPublication.paper_id == drop_id)
            .values(paper_id=keep_id)
        )
    ).rowcount
    report["library_entries_repointed"] = int(entries or 0)
    report["publications_repointed"] = int(publications or 0)

    # ---- 7. 全文分段：keep 没有分段而 drop 有 → 整体迁移（否则随 drop 级联删除） ----
    keep_chunks = (
        await session.execute(
            select(func.count()).select_from(PaperChunk).where(PaperChunk.paper_id == keep_id)
        )
    ).scalar_one()
    chunks_moved = 0
    if not keep_chunks:
        chunks_moved = int(
            (
                await session.execute(
                    update(PaperChunk)
                    .where(PaperChunk.paper_id == drop_id)
                    .values(paper_id=keep_id)
                )
            ).rowcount
            or 0
        )
    report["chunks_moved"] = chunks_moved

    # ---- 8. 内容池行缺项回填（keep 缺 → 用 drop 的） ----
    filled: list[str] = []
    for field in _FILLABLE_PAPER_FIELDS:
        if getattr(keep, field) is None and getattr(drop, field) is not None:
            setattr(keep, field, getattr(drop, field))
            filled.append(field)
    if drop.external_ids:
        keep.external_ids = dict(drop.external_ids) | dict(keep.external_ids or {})
    report["fields_filled"] = filled

    # ---- 9. 删除 drop 行（dedup_key 随行消失；FK 级联清理剩余从属行） ----
    await session.delete(drop)
    await session.commit()
    return report


# ---- 重复候选发现（GET /libraries/{id}/duplicate-candidates） ----


def _candidate_row(paper: Paper, membership: LibraryPaper, chunk_count: int) -> dict[str, Any]:
    return {
        "id": paper.id,
        "title": paper.title,
        "year": paper.year,
        "source": paper.source,
        "arxiv_id": paper.arxiv_id,
        "doi": paper.doi,
        "status": membership.status,
        "chunk_count": chunk_count,
        "has_wiki": bool(membership.wiki_content),
        "created_at": paper.created_at,
    }


async def duplicate_candidates(
    session: AsyncSession, *, library_id: uuid.UUID
) -> list[dict[str, Any]]:
    """库内重复候选（简单实现）：arxiv/doi 同源不同行，或规范化标题相同。

    每组内按「更完整优先」排序（有 wiki > 分段多 > 入库早），首行即建议保留行；
    同一对论文只报一次（先按 arxiv/doi 硬键，再按标题兜底）。
    """
    from app.services.dedup import normalize_title  # 复用全平台标题规范化

    pairs = (
        await session.execute(
            select(Paper, LibraryPaper)
            .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
            .where(LibraryPaper.library_id == library_id)
        )
    ).all()
    if not pairs:
        return []
    counts = dict(
        (
            await session.execute(
                select(PaperChunk.paper_id, func.count())
                .where(PaperChunk.paper_id.in_([p.id for p, _ in pairs]))
                .group_by(PaperChunk.paper_id)
            )
        ).all()
    )

    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for paper, membership in pairs:
        row = _candidate_row(paper, membership, int(counts.get(paper.id, 0)))
        keys: list[tuple[str, str]] = []
        if paper.arxiv_id:
            keys.append(("arxiv", paper.arxiv_id.lower()))
        if paper.doi:
            keys.append(("doi", paper.doi.lower()))
        if title_key := normalize_title(paper.title):
            keys.append(("title", title_key))
        for key in keys:
            buckets.setdefault(key, []).append(row)

    groups: list[dict[str, Any]] = []
    seen_sets: list[set[uuid.UUID]] = []
    for (reason, _), rows in sorted(
        buckets.items(), key=lambda kv: ("arxiv", "doi", "title").index(kv[0][0])
    ):
        unique = {row["id"]: row for row in rows}
        if len(unique) < 2:
            continue
        ids = set(unique)
        if any(ids <= prior for prior in seen_sets):
            continue  # 同一组已按更硬的键报过
        seen_sets.append(ids)
        ordered = sorted(
            unique.values(),
            key=lambda r: (not r["has_wiki"], -r["chunk_count"], r["created_at"]),
        )
        groups.append({"reason": reason, "papers": ordered})
    return groups
