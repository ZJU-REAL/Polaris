"""个人文献库：浏览记录 upsert、收藏、列表检索、回收站（用户级，方向无关）。

回收站 = trashed_at 软删：取消收藏 / 删除条目落 trashed_at，条目从所有 tab（含
浏览记录）里消失，可召回（回到收藏）、可彻底删除、可清空。
"""

import json
import uuid
from typing import cast

from sqlalchemy import Text as SAText
from sqlalchemy import cast as sa_cast
from sqlalchemy import delete, exists, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.library import UserLibraryEntry
from app.models.paper import Paper, PaperUserMeta, UserPaperTag
from app.services.dedup import dedup_key_for

LIBRARY_SORTS = ("recent", "title", "visits", "year")
LIBRARY_TABS = ("saved", "history", "trash")


def dedup_key_for_paper(paper: Paper) -> str:
    """跨方向去重键：arxiv → doi → 规范化标题 sha1（优先级递减，纯标题口径兼容存量键）。"""
    key = dedup_key_for(arxiv_id=paper.arxiv_id, doi=paper.doi, title=paper.title)
    assert key is not None  # Paper.title 非空
    return key


def _snapshot_fields(paper: Paper) -> dict:
    # wiki_content 用 getattr：调用方可能传裸 Paper（内容池行没有 wiki 字段，
    # 库版 wiki 在成员行上，由 PaperView / _SnapshotPaper 包装提供）
    return {
        "arxiv_id": paper.arxiv_id,
        "doi": paper.doi,
        "title": paper.title,
        "authors": paper.authors,
        "year": paper.year,
        "venue": paper.venue,
        "abstract": paper.abstract,
        "url": paper.url,
        "tldr": paper.tldr,
        "wiki_content": getattr(paper, "wiki_content", None),
        "last_paper_id": paper.id,
    }


async def get_entry(
    session: AsyncSession, *, user_id: uuid.UUID, entry_id: uuid.UUID
) -> UserLibraryEntry | None:
    entry = await session.get(UserLibraryEntry, entry_id)
    if entry is None or entry.user_id != user_id:
        return None
    return entry


async def entry_for_paper(
    session: AsyncSession, *, user_id: uuid.UUID, paper: Paper
) -> UserLibraryEntry | None:
    stmt = select(UserLibraryEntry).where(
        UserLibraryEntry.user_id == user_id,
        UserLibraryEntry.dedup_key == dedup_key_for_paper(paper),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def record_visit(
    session: AsyncSession, *, user_id: uuid.UUID, paper: Paper
) -> UserLibraryEntry:
    """阅读页打开时上报：不存在则建条目拷快照，已存在则累计并刷新快照。"""
    entry = await entry_for_paper(session, user_id=user_id, paper=paper)
    if entry is None:
        entry = UserLibraryEntry(
            user_id=user_id, dedup_key=dedup_key_for_paper(paper), **_snapshot_fields(paper)
        )
        session.add(entry)
    else:
        for field, value in _snapshot_fields(paper).items():
            # wiki 快照只升不清：论文当前没有库版 wiki 时保留旧快照
            # （可能是个人编译版 / 库版被删前的快照，P5b 三层解析的兜底层）
            if field == "wiki_content" and value is None:
                continue
            setattr(entry, field, value)
    entry.visit_count = (entry.visit_count or 0) + 1  # 未 flush 的新对象默认值尚未生效
    entry.last_visited_at = utcnow()
    # 又打开了 = 又要看它：条目出回收站（否则新增的浏览在任何 tab 里都看不到）
    entry.trashed_at = None
    await session.commit()
    await session.refresh(entry)
    return entry


async def save_paper(
    session: AsyncSession, *, user_id: uuid.UUID, paper: Paper
) -> UserLibraryEntry:
    """收藏：条目不存在时先建（不计浏览次数），存在则置位 saved。"""
    entry = await entry_for_paper(session, user_id=user_id, paper=paper)
    if entry is None:
        entry = UserLibraryEntry(
            user_id=user_id, dedup_key=dedup_key_for_paper(paper), **_snapshot_fields(paper)
        )
        session.add(entry)
    return await set_saved(session, entry=entry, saved=True)


async def set_saved(
    session: AsyncSession, *, entry: UserLibraryEntry, saved: bool
) -> UserLibraryEntry:
    """收藏 / 取消收藏。

    取消收藏 = 移入回收站（trashed_at 置位），条目从所有 tab 里消失但可召回；
    收藏（含从回收站召回）反过来清 trashed_at。saved_at 语义不变（只标收藏时间）。
    """
    entry.saved = saved
    entry.saved_at = utcnow() if saved else None
    entry.trashed_at = None if saved else utcnow()
    await session.commit()
    await session.refresh(entry)
    return entry


async def purge_trash(session: AsyncSession, *, user_id: uuid.UUID) -> int:
    """清空回收站：彻底删除本人全部软删条目，返回删除条数。"""
    rows = (
        (
            await session.execute(
                select(UserLibraryEntry).where(
                    UserLibraryEntry.user_id == user_id,
                    UserLibraryEntry.trashed_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for entry in rows:
        await session.delete(entry)
    await session.commit()
    return len(rows)


async def set_personal_wiki(
    session: AsyncSession, *, user_id: uuid.UUID, paper: Paper, wiki_content: str
) -> UserLibraryEntry:
    """把个人编译版 wiki 写进本人条目（无条目则建，不动 saved/浏览统计）。"""
    entry = await entry_for_paper(session, user_id=user_id, paper=paper)
    if entry is None:
        entry = UserLibraryEntry(
            user_id=user_id, dedup_key=dedup_key_for_paper(paper), **_snapshot_fields(paper)
        )
        session.add(entry)
    entry.wiki_content = wiki_content
    await session.commit()
    await session.refresh(entry)
    return entry


async def personal_wiki_map(
    session: AsyncSession, *, user_id: uuid.UUID, papers: list[Paper]
) -> dict[uuid.UUID, str]:
    """paper_id → 本人条目里的 wiki（个人编译版 / 历史快照），无则不在结果里。

    P5b 三层解析（库版实时 > 个人版 > 书架快照）的中间层数据源。
    """
    keys = {paper.id: dedup_key_for_paper(paper) for paper in papers}
    if not keys:
        return {}
    stmt = select(UserLibraryEntry.dedup_key, UserLibraryEntry.wiki_content).where(
        UserLibraryEntry.user_id == user_id,
        UserLibraryEntry.dedup_key.in_(set(keys.values())),
        UserLibraryEntry.wiki_content.is_not(None),
    )
    by_key = {key: content for key, content in (await session.execute(stmt)).all() if content}
    return {pid: by_key[key] for pid, key in keys.items() if key in by_key}


async def personal_paper_ids(
    session: AsyncSession, *, user_id: uuid.UUID, tab: str = "saved"
) -> list[uuid.UUID]:
    """个人库里能跳回活体论文的 paper_id 集合（last_paper_id 非空）。

    tab="saved" 只取收藏条目；否则取全部有软引用的条目（回收站里的一律不算）。按收藏
    时间→建条目时间倒序（新→旧），对齐 shelf_paper_ids 形态，供个人库对话按论文集合检索用。
    """
    stmt = select(UserLibraryEntry.last_paper_id).where(
        UserLibraryEntry.user_id == user_id,
        UserLibraryEntry.last_paper_id.is_not(None),
        UserLibraryEntry.trashed_at.is_(None),
    )
    if tab == "saved":
        stmt = stmt.where(UserLibraryEntry.saved.is_(True))
    recency = func.coalesce(UserLibraryEntry.saved_at, UserLibraryEntry.created_at)
    stmt = stmt.order_by(recency.desc())
    return list((await session.execute(stmt)).scalars().all())


async def semantic_saved_entries(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    query_vector: list[float],
    limit: int,
) -> list[tuple[UserLibraryEntry, float]]:
    """对本人收藏、且软引用论文有向量的条目跑 pgvector 余弦，返回 (条目, 相似度) 降序。

    候选集 = saved 且 last_paper_id 非空的条目里，其 Paper.embedding 非空的那些
    （覆盖不全的已知限制：没生成向量的收藏不会命中）。仅 postgres，调用方需先判
    semantic_search_supported。条目自身带 title/authors 快照，命中即可直接渲染成个人库行。
    """
    stmt = select(UserLibraryEntry).where(
        UserLibraryEntry.user_id == user_id,
        UserLibraryEntry.saved.is_(True),
        UserLibraryEntry.last_paper_id.is_not(None),
    )
    entries = list((await session.execute(stmt)).scalars().all())
    by_pid: dict[uuid.UUID, UserLibraryEntry] = {
        e.last_paper_id: e for e in entries if e.last_paper_id is not None
    }
    if not by_pid:
        return []
    qv = json.dumps(query_vector)
    rows = (
        await session.execute(
            text(
                "SELECT p.id, 1 - (p.embedding <=> CAST(:qv AS vector)) AS score "
                "FROM papers p "
                "WHERE p.id = ANY(CAST(:ids AS uuid[])) AND p.embedding IS NOT NULL "
                "ORDER BY score DESC "
                "LIMIT :k"
            ),
            {"qv": qv, "ids": [str(pid) for pid in by_pid], "k": limit},
        )
    ).all()
    return [(by_pid[row.id], float(row.score)) for row in rows if row.id in by_pid]


async def purge_entry(session: AsyncSession, *, entry: UserLibraryEntry) -> None:
    await session.delete(entry)
    await session.commit()


async def clear_history(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    """清空浏览记录：未收藏条目删除，已收藏条目保留但清零访问统计。

    回收站里的条目一律不动——它们已经不在浏览记录里，清历史不该顺手清空回收站。
    """
    await session.execute(
        delete(UserLibraryEntry).where(
            UserLibraryEntry.user_id == user_id,
            UserLibraryEntry.saved.is_(False),
            UserLibraryEntry.trashed_at.is_(None),
        )
    )
    await session.execute(
        update(UserLibraryEntry)
        .where(
            UserLibraryEntry.user_id == user_id,
            UserLibraryEntry.trashed_at.is_(None),
        )
        .values(visit_count=0, last_visited_at=None)
    )
    await session.commit()


async def list_entries(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    tab: str = "history",
    q: str | None = None,
    sort: str = "recent",
    page: int = 1,
    size: int = 20,
    year_from: int | None = None,
    year_to: int | None = None,
    author: str | None = None,
    affiliation: str | None = None,
    venue: str | None = None,
    reading_status: str | None = None,
    starred: bool | None = None,
    my_tag: str | None = None,
) -> tuple[list[UserLibraryEntry], int]:
    """分页列个人库条目（tab 决定收藏 / 浏览记录 / 回收站），带高级检索过滤。

    star / 阅读状态 / 我的标签这三项是论文维度的个人属性，条目快照里没有，只能靠
    ``last_paper_id`` 软引用去查（同 affiliation 的口径）。**源论文已删（last_paper_id
    为空）的条目一律筛不出来**——包括 reading_status=unread：没有论文就无从判断读没读，
    把一堆无源快照按「未读」倒进结果里只会淹掉真正没读的论文。论文还在、只是从没标过
    状态的，仍按「未读」算（沿用 papers.apply_paper_filters 的口径）。
    """
    stmt = select(UserLibraryEntry).where(UserLibraryEntry.user_id == user_id)
    # 回收站条目只出现在 trash tab；收藏 / 浏览记录都不含（否则删掉的会从浏览记录漏回来）
    if tab == "trash":
        stmt = stmt.where(UserLibraryEntry.trashed_at.is_not(None))
    elif tab == "saved":
        stmt = stmt.where(
            UserLibraryEntry.saved.is_(True), UserLibraryEntry.trashed_at.is_(None)
        )
    else:  # history：看过的条目（收藏但从未打开过的不算浏览记录）
        stmt = stmt.where(
            UserLibraryEntry.visit_count > 0, UserLibraryEntry.trashed_at.is_(None)
        )
    if q:
        pattern = f"%{q}%"
        stmt = stmt.where(
            or_(
                UserLibraryEntry.title.ilike(pattern),
                sa_cast(UserLibraryEntry.authors, SAText).ilike(pattern),
                UserLibraryEntry.venue.ilike(pattern),
            )
        )
    # 高级检索：作者在 JSON 列上做文本包含匹配（同 q 里的作者匹配口径）
    if author:
        stmt = stmt.where(sa_cast(UserLibraryEntry.authors, SAText).ilike(f"%{author}%"))
    # 机构：条目快照里没有机构字段，借软引用的活体论文（last_paper_id）匹配。
    # 源论文已删的条目匹配不上——详情面板也只在论文还在时才显示机构，口径一致。
    if affiliation:
        stmt = stmt.where(
            UserLibraryEntry.last_paper_id.in_(
                select(Paper.id).where(
                    sa_cast(Paper.affiliations, SAText).ilike(f"%{affiliation}%")
                )
            )
        )
    if venue:
        stmt = stmt.where(UserLibraryEntry.venue.ilike(f"%{venue}%"))
    # 星标 / 阅读状态 / 我的标签：同样是论文那边的个人属性，按 last_paper_id 软引用查；
    # 源论文已删的条目（last_paper_id 为空）在这三个过滤下一律不出现。
    if starred is not None:
        starred_exists = exists().where(
            PaperUserMeta.paper_id == UserLibraryEntry.last_paper_id,
            PaperUserMeta.user_id == user_id,
            PaperUserMeta.starred.is_(True),
        )
        stmt = stmt.where(
            UserLibraryEntry.last_paper_id.is_not(None),
            starred_exists if starred else ~starred_exists,
        )
    if reading_status:
        status_sub = (
            select(PaperUserMeta.reading_status)
            .where(
                PaperUserMeta.paper_id == UserLibraryEntry.last_paper_id,
                PaperUserMeta.user_id == user_id,
            )
            .scalar_subquery()
        )
        stmt = stmt.where(
            UserLibraryEntry.last_paper_id.is_not(None),
            func.coalesce(status_sub, "unread") == reading_status,
        )
    if my_tag:
        stmt = stmt.where(
            exists().where(
                UserPaperTag.paper_id == UserLibraryEntry.last_paper_id,
                UserPaperTag.user_id == user_id,
                UserPaperTag.name == my_tag,
            )
        )
    if year_from is not None:
        stmt = stmt.where(UserLibraryEntry.year.isnot(None), UserLibraryEntry.year >= year_from)
    if year_to is not None:
        stmt = stmt.where(UserLibraryEntry.year.isnot(None), UserLibraryEntry.year <= year_to)
    if tab == "trash":  # 回收站固定按移入时间倒序（最近删的在前）
        stmt = stmt.order_by(UserLibraryEntry.trashed_at.desc())
    elif sort == "title":
        stmt = stmt.order_by(UserLibraryEntry.title.asc())
    elif sort == "visits":
        stmt = stmt.order_by(
            UserLibraryEntry.visit_count.desc(), UserLibraryEntry.last_visited_at.desc()
        )
    elif sort == "year":
        stmt = stmt.order_by(
            UserLibraryEntry.year.desc().nulls_last(), UserLibraryEntry.created_at.desc()
        )
    else:  # recent：收藏 tab 里没浏览过的条目按收藏时间排
        recency = func.coalesce(
            UserLibraryEntry.last_visited_at,
            UserLibraryEntry.saved_at,
            UserLibraryEntry.created_at,
        )
        stmt = stmt.order_by(recency.desc())
    total = cast(
        int,
        (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one(),
    )
    rows = (await session.execute(stmt.offset((page - 1) * size).limit(size))).scalars().all()
    return list(rows), total
