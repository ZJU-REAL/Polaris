"""个人文献库：浏览记录 upsert、收藏、列表检索（用户级，方向无关）。"""

import uuid
from typing import cast

from sqlalchemy import Text as SAText
from sqlalchemy import cast as sa_cast
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.library import UserLibraryEntry
from app.models.paper import Paper
from app.services.dedup import dedup_key_for

LIBRARY_SORTS = ("recent", "title", "visits", "year")
LIBRARY_TABS = ("saved", "history")


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
    entry.saved = saved
    entry.saved_at = utcnow() if saved else None
    await session.commit()
    await session.refresh(entry)
    return entry


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

    tab="saved" 只取收藏条目；否则取全部有软引用的条目。按收藏时间→建条目时间
    倒序（新→旧），对齐 shelf_paper_ids 形态，供个人库对话按论文集合检索用。
    """
    stmt = select(UserLibraryEntry.last_paper_id).where(
        UserLibraryEntry.user_id == user_id,
        UserLibraryEntry.last_paper_id.is_not(None),
    )
    if tab == "saved":
        stmt = stmt.where(UserLibraryEntry.saved.is_(True))
    recency = func.coalesce(UserLibraryEntry.saved_at, UserLibraryEntry.created_at)
    stmt = stmt.order_by(recency.desc())
    return list((await session.execute(stmt)).scalars().all())


async def purge_entry(session: AsyncSession, *, entry: UserLibraryEntry) -> None:
    await session.delete(entry)
    await session.commit()


async def clear_history(session: AsyncSession, *, user_id: uuid.UUID) -> None:
    """清空浏览记录：未收藏条目删除，已收藏条目保留但清零访问统计。"""
    await session.execute(
        delete(UserLibraryEntry).where(
            UserLibraryEntry.user_id == user_id, UserLibraryEntry.saved.is_(False)
        )
    )
    await session.execute(
        update(UserLibraryEntry)
        .where(UserLibraryEntry.user_id == user_id)
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
    venue: str | None = None,
) -> tuple[list[UserLibraryEntry], int]:
    stmt = select(UserLibraryEntry).where(UserLibraryEntry.user_id == user_id)
    if tab == "saved":
        stmt = stmt.where(UserLibraryEntry.saved.is_(True))
    else:  # history：看过的条目（收藏但从未打开过的不算浏览记录）
        stmt = stmt.where(UserLibraryEntry.visit_count > 0)
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
    if venue:
        stmt = stmt.where(UserLibraryEntry.venue.ilike(f"%{venue}%"))
    if year_from is not None:
        stmt = stmt.where(UserLibraryEntry.year.isnot(None), UserLibraryEntry.year >= year_from)
    if year_to is not None:
        stmt = stmt.where(UserLibraryEntry.year.isnot(None), UserLibraryEntry.year <= year_to)
    if sort == "title":
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
