"""重复论文合并（docs-dev/rfc-paper-content-pool.md §5）。

模糊去重的漏网（预印本 vs 正式版、标题变体）由用户人工确认合并：把 drop 行的
全部归属 repoint 到 keep 行后删除 drop。所有归属只经 paper_id 引用，合并是局部操作。

约定：
- 冲突表（同库 / 同课题 / 同用户已各有一行）按「keep 行优先、缺项用 drop 补」合并；
- keep 行缺全文 / 图 / 元数据 / 解读时从 drop 行搬运（chunks 仅在 keep 无分段时整体迁移）；
- 其余带唯一约束的从属表（PDF 资产、解析版本、结构化抽取、证据锚点、个人标签、
  方法向量、下载任务、每日池条目）逐行搬：keep 侧没有同键行的搬过去，有的留给级联删除；
- drop 的 dedup_key 无法与 keep 并存（UNIQUE），随行删除并写进返回报告；
- 全程一个事务，最后统一 commit；paper 不存在或 keep==drop 抛 ValueError。
"""

import logging
import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models.daily_feed import DailyFeedEntry
from app.models.download_client import DownloadBatchItem
from app.models.evidence import PaperEvidenceAnchor
from app.models.library import UserLibraryEntry
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.literature_discovery import LiteratureSearchHit
from app.models.paper import (
    Paper,
    PaperChunk,
    PaperHighlight,
    PaperNote,
    PaperUserMeta,
    PaperWiki,
    UserPaperTag,
    paper_concepts,
    paper_tag_links,
)
from app.models.paper_assets import PaperAsset
from app.models.paper_citation import PaperCitation
from app.models.paper_content import PaperContentVersion
from app.models.paper_extraction import PaperExtraction
from app.models.publication import UserPublication
from app.models.topic_shelf import TopicPaper
from app.models.vectors import MethodVector, PaperVector
from app.services.dedup import WorkRef, same_work, title_keys

logger = logging.getLogger(__name__)

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
    # 向量不在这张表上（paper_vectors，每个空间一行），按空间逐个补，见 _merge_paper_vectors
    "affiliations",
    "authors",
)

# 成员行上的判断字段：同库冲突时 keep 缺则补（解读不在成员行上，另行合并）
_FILLABLE_MEMBERSHIP_FIELDS = (
    "relevance_score",
    "tldr_note",
    "scored_at",
    "scored_run_id",  # 跟着 scored_at 走，合并后仍认得出是哪次同步收下的
)

# 阅读状态推进序（冲突时取两边更靠后的）
_READING_ORDER = {"unread": 0, "reading": 1, "read": 2}
# 成员行状态推进序（excluded/included 人工态不参与自动升级）
_STATUS_ORDER = {"candidate": 0, "scored": 1, "fetched": 2, "compiled": 3}


async def _merge_paper_vectors(
    session: AsyncSession, *, keep_id: uuid.UUID, drop_id: uuid.UUID
) -> int:
    """把 drop 独有空间的论文级向量搬给 keep，返回搬了几条。

    按**空间**逐个补：keep 在某个空间下已经有向量就保留自己的（它是这一行的主体），
    只有 keep 缺、drop 有的空间才搬过来。剩下的 drop 向量随 drop 行级联删除。
    """
    keep_spaces = set(
        (await session.execute(select(PaperVector.space).where(PaperVector.paper_id == keep_id)))
        .scalars()
        .all()
    )
    moved = 0
    rows = (
        (await session.execute(select(PaperVector).where(PaperVector.paper_id == drop_id)))
        .scalars()
        .all()
    )
    for row in rows:
        if row.space in keep_spaces:
            continue
        row.paper_id = keep_id
        moved += 1
    return moved


# 带唯一约束的从属表：(报告里的名字, 模型, 唯一键里除 paper_id 之外的列)。keep 侧已有
# 同键行时 drop 的那行不搬（随 drop 级联删除）——键为空元组表示「每篇论文至多一行」。
_KEYED_DEPENDENTS: tuple[tuple[str, Any, tuple[str, ...]], ...] = (
    ("extractions_moved", PaperExtraction, ("schema_id",)),
    ("evidence_anchors_moved", PaperEvidenceAnchor, ("content_revision", "anchor_key")),
    ("user_tags_moved", UserPaperTag, ("user_id", "name")),
    ("method_vectors_moved", MethodVector, ("axis", "schema_id", "space")),
    ("download_items_moved", DownloadBatchItem, ("batch_id", "library_id")),
    ("daily_feed_entries_moved", DailyFeedEntry, ()),
)


async def _move_keyed_rows(
    session: AsyncSession,
    model: Any,
    key_attrs: tuple[str, ...],
    *,
    keep_id: uuid.UUID,
    drop_id: uuid.UUID,
) -> list[Any]:
    """把 drop 的行改挂到 keep 上，跳过 keep 已有同键行的；返回搬过去的行。"""

    def key_of(row: Any) -> tuple[Any, ...]:
        return tuple(getattr(row, attr) for attr in key_attrs)

    taken = {
        key_of(row)
        for row in (await session.execute(select(model).where(model.paper_id == keep_id))).scalars()
    }
    moved: list[Any] = []
    for row in (await session.execute(select(model).where(model.paper_id == drop_id))).scalars():
        if key_of(row) in taken:
            continue
        taken.add(key_of(row))
        row.paper_id = keep_id
        moved.append(row)
    return moved


async def _move_assets_and_content(
    session: AsyncSession, *, keep_id: uuid.UUID, drop_id: uuid.UUID
) -> tuple[int, int]:
    """PDF 资产和挂在它上面的解析版本一起搬，返回 (资产数, 解析版本数)。

    keep 已经有首选资产 / 当前版本时，搬过来的不抢这个位置；版本号接在 keep 现有的
    后面（(paper_id, version_no) 唯一）。资产没搬走的解析版本不搬——它引用的资产会
    随 drop 删除，版本跟着级联消失。
    """
    keep_has_preferred = (
        await session.execute(
            select(PaperAsset.id)
            .where(PaperAsset.paper_id == keep_id, PaperAsset.is_preferred.is_(True))
            .limit(1)
        )
    ).first() is not None
    assets = await _move_keyed_rows(
        session, PaperAsset, ("blob_id", "source"), keep_id=keep_id, drop_id=drop_id
    )
    for asset in assets:
        if keep_has_preferred:
            asset.is_preferred = False
        keep_has_preferred = keep_has_preferred or asset.is_preferred
    moved_asset_ids = {asset.id for asset in assets}

    keep_versions = list(
        (
            await session.execute(
                select(PaperContentVersion).where(PaperContentVersion.paper_id == keep_id)
            )
        ).scalars()
    )
    next_no = max((v.version_no for v in keep_versions), default=0) + 1
    keep_has_current = any(v.is_current for v in keep_versions)
    versions = 0
    for version in (
        await session.execute(
            select(PaperContentVersion)
            .where(PaperContentVersion.paper_id == drop_id)
            .order_by(PaperContentVersion.version_no)
        )
    ).scalars():
        if version.asset_id not in moved_asset_ids:
            continue
        version.paper_id = keep_id
        version.version_no = next_no
        next_no += 1
        if keep_has_current:
            version.is_current = False
        keep_has_current = keep_has_current or version.is_current
        versions += 1
    return len(assets), versions


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
        await session.execute(delete(table).where(paper_col == drop_id, other_col.in_(dup_subq)))
    ).rowcount
    repointed = (
        await session.execute(update(table).where(paper_col == drop_id).values(paper_id=keep_id))
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

    # ---- 1b. paper_wikis：一篇一份，keep 没有解读时把 drop 的搬过来 ----
    keep_wiki = (
        await session.execute(select(PaperWiki).where(PaperWiki.paper_id == keep_id))
    ).scalar_one_or_none()
    drop_wiki = (
        await session.execute(select(PaperWiki).where(PaperWiki.paper_id == drop_id))
    ).scalar_one_or_none()
    wiki_moved = False
    if drop_wiki is not None:
        if keep_wiki is None:
            drop_wiki.paper_id = keep_id
            set_committed_value(keep, "wiki", drop_wiki)
            wiki_moved = True
        else:  # 两边都有 → 保留 keep 的那份（drop 行随论文删除）
            await session.delete(drop_wiki)
        # 从 drop 上摘掉：否则删 drop 时 delete-orphan 会把刚搬走的那行一并删掉
        set_committed_value(drop, "wiki", None)
    report["wiki_moved"] = wiki_moved

    # ---- 2. topic_papers：同课题冲突保留 keep 行（缺备注则补），否则 repoint ----
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
        # wiki 快照列已退役删除（#734）：解读统一走 paper_wikis，无需搬运
        if not existing.note and row.note:
            existing.note = row.note
        # 一边在架一边在回收站 → 合并后在架（别让合并把还在书架上的条目变没）
        if existing.trashed_at is not None and row.trashed_at is None:
            existing.trashed_at = None
            existing.trashed_by = None
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
    # 分段向量以 chunk_id 为键，随分段一起搬走，不用单独处理；论文级向量按空间补
    report["vectors_moved"] = await _merge_paper_vectors(session, keep_id=keep_id, drop_id=drop_id)

    # ---- 7b. 其余指向这篇论文的行（逐表列全，漏一张就是合并时悄悄丢数据） ----
    assets_moved, versions_moved = await _move_assets_and_content(
        session, keep_id=keep_id, drop_id=drop_id
    )
    report["assets_moved"] = assets_moved
    report["content_versions_moved"] = versions_moved
    for name, model, key_attrs in _KEYED_DEPENDENTS:
        report[name] = len(
            await _move_keyed_rows(session, model, key_attrs, keep_id=keep_id, drop_id=drop_id)
        )
    # 参考文献表：两份各自编号，掺在一起就乱了——keep 没有才整份搬（同全文分段）
    keep_refs = (
        await session.execute(
            select(func.count())
            .select_from(PaperCitation)
            .where(PaperCitation.citing_paper_id == keep_id)
        )
    ).scalar_one()
    references_moved = 0
    if not keep_refs:
        references_moved = int(
            (
                await session.execute(
                    update(PaperCitation)
                    .where(PaperCitation.citing_paper_id == drop_id)
                    .values(citing_paper_id=keep_id)
                )
            ).rowcount
            or 0
        )
    report["references_moved"] = references_moved
    # 别的论文引用 drop 的边、检索候选的「已入库」指针：没有唯一约束，直接改指 keep
    # （不改的话删 drop 时会被置空，引用关系和「这条候选已入库」的记录就断了）
    report["cited_by_repointed"] = int(
        (
            await session.execute(
                update(PaperCitation)
                .where(PaperCitation.cited_paper_id == drop_id)
                .values(cited_paper_id=keep_id)
            )
        ).rowcount
        or 0
    )
    report["search_hits_repointed"] = int(
        (
            await session.execute(
                update(LiteratureSearchHit)
                .where(LiteratureSearchHit.paper_id == drop_id)
                .values(paper_id=keep_id)
            )
        ).rowcount
        or 0
    )

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
        "has_wiki": paper.wiki is not None,
        "created_at": paper.created_at,
    }


async def duplicate_candidates(
    session: AsyncSession, *, library_id: uuid.UUID
) -> list[dict[str, Any]]:
    """库内重复候选（简单实现）：arxiv/doi 同源不同行，或规范化标题相同
    （含一边多一个开头代号的写法）。这里只看标题、不核对年份和作者——是给人过目的
    候选；自动合并用更严的规则，见 :func:`merge_library_duplicates`。

    每组内按「更完整优先」排序（有 wiki > 分段多 > 入库早），首行即建议保留行；
    同一对论文只报一次（先按 arxiv/doi 硬键，再按标题兜底）。
    """
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
        # 整题，以及去掉开头代号后的部分（"REALM: X" 和 "X" 归到一组给人看）
        keys.extend(("title", key) for key in dict.fromkeys(title_keys(paper.title)) if key)
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


# ---- 库内重复自动合并 ----

# 一轮合并后 keep 可能从 drop 那里补到新标识，又和第三行对上；多跑几轮直到不再有合并
_MAX_MERGE_PASSES = 5


def _richness(paper: Paper, chunk_count: int) -> tuple[Any, ...]:
    """「哪一行更完整」的排序键（越小越该留）：有解读 > 有全文 > 有 PDF > 标识多 >
    有摘要 > 入池早。"""
    identifiers = sum(1 for v in (paper.arxiv_id, paper.doi) if v) + len(paper.external_ids or {})
    return (
        paper.wiki is None,
        not (paper.full_text_path or chunk_count),
        not paper.pdf_path,
        -identifiers,
        not paper.abstract,
        paper.created_at,
        str(paper.id),
    )


def _is_duplicate(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """两行是不是同一篇：arXiv 号相同、DOI 相同，或者按 dedup.same_work 判定。
    两边各有 arXiv 号且不同，一律不是。"""
    if a["arxiv"] and b["arxiv"]:
        return a["arxiv"] == b["arxiv"]
    if a["doi"] and a["doi"] == b["doi"]:
        return True
    return same_work(a["ref"], b["ref"])


async def _merge_pass(session: AsyncSession, library_id: uuid.UUID) -> list[dict[str, Any]]:
    papers = list(
        (
            await session.execute(
                select(Paper)
                .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
                .where(LibraryPaper.library_id == library_id)
            )
        ).scalars()
    )
    if len(papers) < 2:
        return []
    counts = dict(
        (
            await session.execute(
                select(PaperChunk.paper_id, func.count())
                .where(PaperChunk.paper_id.in_([p.id for p in papers]))
                .group_by(PaperChunk.paper_id)
            )
        ).all()
    )
    # 合并会提交、让 ORM 对象过期，所以先把要用的值都取成普通数据
    rows: list[dict[str, Any]] = []
    for paper in papers:
        ref = WorkRef(
            title=paper.title, year=paper.year, authors=paper.authors, arxiv_id=paper.arxiv_id
        )
        rows.append(
            {
                "id": paper.id,
                "arxiv": ref.bare_arxiv,
                "doi": (paper.doi or "").strip().lower(),
                "ref": ref,
                "title": paper.title,
                "rank": _richness(paper, int(counts.get(paper.id, 0))),
            }
        )
    rows.sort(key=lambda row: row["rank"])
    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        keys = [("title", key) for key in title_keys(row["title"]) if key]
        if row["arxiv"]:
            keys.append(("arxiv", row["arxiv"]))
        if row["doi"]:
            keys.append(("doi", row["doi"]))
        row["keys"] = list(dict.fromkeys(keys))
        for key in row["keys"]:
            buckets.setdefault(key, []).append(row)

    reports: list[dict[str, Any]] = []
    gone: set[uuid.UUID] = set()
    for keep in rows:  # 从最完整的一行开始，它吸收所有和它对得上的
        if keep["id"] in gone:
            continue
        for key in keep["keys"]:
            for other in buckets[key]:
                if other["id"] == keep["id"] or other["id"] in gone:
                    continue
                if not _is_duplicate(keep, other):
                    continue
                report = await merge_papers(session, keep_id=keep["id"], drop_id=other["id"])
                gone.add(other["id"])
                reports.append(report)
                logger.info(
                    "merged duplicate paper %s into %s (%s)",
                    other["id"],
                    keep["id"],
                    str(keep["title"])[:60],
                )
    return reports


async def merge_library_duplicates(
    session: AsyncSession, *, library_id: uuid.UUID
) -> list[dict[str, Any]]:
    """把一个库里已经存在的重复论文合并掉，返回每次合并的报告（没有重复返回空列表）。

    判定比 :func:`duplicate_candidates` 严：arXiv 号相同、DOI 相同，或者标题是同一个
    题目的不同写法 **且** 年份相差不超过 1 **且** 首作者同姓（dedup.same_work）。
    每组留最完整的一行（:func:`_richness`），其余经 :func:`merge_papers` 并进去——
    成员行、笔记、划线、全文、解读、引用关系都跟过去，缺的标识从被合并的行补上。

    可以反复跑：合并完再跑一遍什么都不做。每合并一对提交一次。
    """
    reports: list[dict[str, Any]] = []
    for _ in range(_MAX_MERGE_PASSES):
        merged = await _merge_pass(session, library_id)
        if not merged:
            break
        reports.extend(merged)
    return reports


async def merge_all_library_duplicates(session: AsyncSession) -> int:
    """对每个库跑一遍 :func:`merge_library_duplicates`，返回合并掉的行数。

    一个库出错只记日志、接着处理下一个。
    """
    library_ids = list((await session.execute(select(DirectionLibrary.id))).scalars())
    merged = 0
    for library_id in library_ids:
        try:
            merged += len(await merge_library_duplicates(session, library_id=library_id))
        except Exception:  # noqa: BLE001 — 一个库合并失败不该挡住其余的库
            await session.rollback()
            logger.warning("merging duplicates in library %s failed", library_id, exc_info=True)
    return merged
