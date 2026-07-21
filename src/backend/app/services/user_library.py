"""个人文献库：浏览记录 upsert、收藏、列表检索（用户级，方向无关）。"""

import hashlib
import re
import uuid
from typing import cast

from sqlalchemy import Text as SAText
from sqlalchemy import cast as sa_cast
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.library import UserLibraryEntry
from app.models.paper import Paper

LIBRARY_SORTS = ("recent", "title", "visits")
LIBRARY_TABS = ("saved", "history")


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def dedup_key_for_paper(paper: Paper) -> str:
    """跨方向去重键：arxiv → doi → 规范化标题 sha1（优先级递减）。"""
    if paper.arxiv_id:
        return f"arxiv:{paper.arxiv_id.lower()}"
    if paper.doi:
        return f"doi:{paper.doi.lower()}"
    digest = hashlib.sha1(_normalize_title(paper.title).encode()).hexdigest()
    return f"title:{digest}"


def _snapshot_fields(paper: Paper) -> dict:
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
    if sort == "title":
        stmt = stmt.order_by(UserLibraryEntry.title.asc())
    elif sort == "visits":
        stmt = stmt.order_by(
            UserLibraryEntry.visit_count.desc(), UserLibraryEntry.last_visited_at.desc()
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
