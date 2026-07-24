"""每日新论文池（Daily Paper）业务逻辑。

- 同步：每天从 arxiv 订阅分类抓 New submissions（RSS /new，announce_type∈{new,cross}），
  先查全局内容池去重、无则建轻量 Paper（不下 PDF、不触发 LLM——每天上百篇，重活留给
  收录后的库流程），再 upsert 池 entry；同一篇多分类命中合并进 categories，paper_id
  唯一约束保证同日重跑幂等。
- 滚动 7 天：清理直接删过期 entry（likes 显式跟删，兼容 sqlite 测试无 FK 级联）；
  内容池 Paper 与各库成员表一概不动——收录动作写的是目标库自己的表。
- 点赞：全实验室共享，每人每篇一赞；列表按赞数排序、附前几名点赞人（facepile 用）。
- 收录：分发到现成写路径（方向库 ensure_membership / 课题书架 add_to_shelf /
  个人库 save_paper），无权目标单独标记 forbidden，不整体失败。
"""

import datetime as dt
import logging
import re
import uuid
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.daily_feed import (
    DAILY_FEED_RETENTION_DAYS,
    DEFAULT_DAILY_CATEGORIES,
    DailyFeedEntry,
    DailyFeedLike,
)
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper
from app.models.system_setting import SystemSetting
from app.models.topic_shelf import TopicPaper
from app.models.user import User
from app.services import projects as projects_service
from app.services import topic_shelf as shelf_service
from app.services import user_library
from app.services.dedup import pool_dedup_key
from app.services.libraries import can_manage_library, ensure_membership, find_pool_paper
from app.services.literature import get_arxiv_client
from app.services.paper_import import _parse_iso

logger = logging.getLogger(__name__)

CATEGORIES_SETTING_KEY = "daily_feed_categories"

# arXiv 分类形如 cs.AI / stat.ML / eess.IV / math.OC（主类小写，子类大写字母数字短串）
_CATEGORY_RE = re.compile(r"^[a-z][a-z-]{1,15}(\.[A-Za-z]{2,10})?$")

_MAX_LIKERS_PREVIEW = 5


class DailyEntryNotFoundError(Exception):
    pass


class InvalidCategoryError(Exception):
    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _today_utc() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


# ---- 订阅分类配置 ----


async def get_categories(session: AsyncSession) -> list[str]:
    row = await session.get(SystemSetting, CATEGORIES_SETTING_KEY)
    if row is None or not isinstance(row.value, list) or not row.value:
        return list(DEFAULT_DAILY_CATEGORIES)
    return [str(c) for c in row.value]


async def set_categories(session: AsyncSession, categories: list[str]) -> list[str]:
    cleaned: list[str] = []
    for raw in categories:
        cat = raw.strip()
        if not cat:
            continue
        if not _CATEGORY_RE.match(cat):
            raise InvalidCategoryError(cat)
        if cat not in cleaned:
            cleaned.append(cat)
    if not cleaned:
        raise InvalidCategoryError("(empty)")
    row = await session.get(SystemSetting, CATEGORIES_SETTING_KEY)
    if row is None:
        session.add(SystemSetting(key=CATEGORIES_SETTING_KEY, value=cleaned))
    else:
        row.value = cleaned
    await session.commit()
    return cleaned


# ---- 每日同步（cron / 手动刷新） ----


def _make_pool_paper(entry: dict[str, Any]) -> Paper:
    """RSS entry → 轻量内容池 Paper（不下 PDF、不补机构——feed 量大，重活留给收录后）。"""
    aid = entry.get("arxiv_id")
    return Paper(
        source="arxiv",
        dedup_key=pool_dedup_key(
            arxiv_id=aid,
            doi=entry.get("doi"),
            title=entry["title"],
            year=entry.get("year"),
            authors=entry.get("authors"),
        ),
        arxiv_id=aid,
        doi=entry.get("doi"),
        external_ids={"arxiv": aid} if aid else None,
        title=entry["title"],
        authors=entry.get("authors"),
        abstract=entry.get("abstract"),
        year=entry.get("year"),
        venue=entry.get("primary_category"),
        url=entry.get("url"),
        published_at=_parse_iso(entry.get("published")),
    )


async def cleanup_expired(session: AsyncSession, *, today: dt.date | None = None) -> int:
    """删过期 entry（保留含今天共 RETENTION 天）；likes 显式跟删（不依赖 DB 级联）。"""
    cutoff = (today or _today_utc()) - dt.timedelta(days=DAILY_FEED_RETENTION_DAYS - 1)
    expired_ids = select(DailyFeedEntry.id).where(DailyFeedEntry.feed_date < cutoff)
    await session.execute(delete(DailyFeedLike).where(DailyFeedLike.entry_id.in_(expired_ids)))
    result = await session.execute(delete(DailyFeedEntry).where(DailyFeedEntry.feed_date < cutoff))
    return result.rowcount or 0


async def sync_daily_feed(session: AsyncSession) -> dict[str, Any]:
    """抓订阅分类的当天新公告入池 + 清理过期；幂等（同日重跑 created=0）。"""
    categories = await get_categories(session)
    today = _today_utc()
    client = get_arxiv_client()
    fetched = created = 0

    for category in categories:
        entries = await client.fetch_new(category)  # 失败已在客户端兜底为 []
        fetched += len(entries)
        for entry in entries:
            title = (entry.get("title") or "").strip()
            arxiv_id = entry.get("arxiv_id")
            if not title or not arxiv_id:
                continue
            paper = await find_pool_paper(
                session,
                arxiv_id=arxiv_id,
                doi=entry.get("doi"),
                dedup_key=pool_dedup_key(
                    arxiv_id=arxiv_id,
                    doi=entry.get("doi"),
                    title=title,
                    year=entry.get("year"),
                    authors=entry.get("authors"),
                ),
            )
            if paper is None:
                paper = _make_pool_paper(entry)
                session.add(paper)
                await session.flush()
            row = (
                await session.execute(
                    select(DailyFeedEntry).where(DailyFeedEntry.paper_id == paper.id)
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    DailyFeedEntry(
                        paper_id=paper.id,
                        feed_date=today,
                        primary_category=category,
                        categories=[category],
                        announce_type=entry.get("announce_type") or "new",
                    )
                )
                created += 1
            elif category not in (row.categories or []):
                # 同日另一分类命中（cross-list）：合并分类，不动 feed_date
                row.categories = [*(row.categories or []), category]

    expired = await cleanup_expired(session, today=today)
    await session.commit()
    return {"fetched": fetched, "created": created, "expired": expired, "categories": categories}


# ---- 池浏览 ----


def _like_count_sq() -> Any:
    return (
        select(func.count(DailyFeedLike.id))
        .where(DailyFeedLike.entry_id == DailyFeedEntry.id)
        .correlate(DailyFeedEntry)
        .scalar_subquery()
    )


async def _likes_by_entry(
    session: AsyncSession, entry_ids: list[uuid.UUID], *, user_id: uuid.UUID
) -> dict[uuid.UUID, dict[str, Any]]:
    """一次查出这页 entry 的全部点赞（含用户名/头像），拼 facepile 预览。"""
    empty = {"like_count": 0, "liked_by_me": False, "likers_preview": []}
    if not entry_ids:
        return {}
    rows = (
        await session.execute(
            select(DailyFeedLike.entry_id, User.id, User.display_name, User.avatar_path)
            .join(User, User.id == DailyFeedLike.user_id)
            .where(DailyFeedLike.entry_id.in_(entry_ids))
            .order_by(DailyFeedLike.created_at.desc())
        )
    ).all()
    out: dict[uuid.UUID, dict[str, Any]] = {
        eid: dict(empty, likers_preview=[]) for eid in entry_ids
    }
    for entry_id, uid, display_name, avatar_path in rows:
        info = out[entry_id]
        info["like_count"] += 1
        liker = {"id": uid, "display_name": display_name, "has_avatar": bool(avatar_path)}
        if uid == user_id:
            info["liked_by_me"] = True
            info["likers_preview"].insert(0, liker)  # 自己永远排最前
        else:
            info["likers_preview"].append(liker)
    for info in out.values():
        info["likers_preview"] = info["likers_preview"][:_MAX_LIKERS_PREVIEW]
    return out


def _entry_item(entry: DailyFeedEntry, paper: Paper, likes: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_id": entry.id,
        "paper_id": paper.id,
        "feed_date": entry.feed_date,
        "primary_category": entry.primary_category,
        "categories": entry.categories or [],
        "announce_type": entry.announce_type,
        "title": paper.title,
        "authors": paper.authors or [],
        "abstract": paper.abstract,
        "year": paper.year,
        "arxiv_id": paper.arxiv_id,
        "url": paper.url,
        "published_at": paper.published_at,
        "has_wiki": bool(entry.wiki_content),
        **likes,
    }


async def list_days(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(DailyFeedEntry.feed_date, func.count(DailyFeedEntry.id))
            .group_by(DailyFeedEntry.feed_date)
            .order_by(DailyFeedEntry.feed_date.desc())
        )
    ).all()
    return [{"date": date, "count": count} for date, count in rows]


async def list_papers(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    date: dt.date | None = None,
    sort: str = "likes",
    page: int = 1,
    size: int = 20,
    q: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(DailyFeedEntry, Paper).join(Paper, Paper.id == DailyFeedEntry.paper_id)
    if date is not None:
        stmt = stmt.where(DailyFeedEntry.feed_date == date)
    if q:
        stmt = stmt.where(Paper.title.ilike(f"%{q.strip()}%"))

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    likes_sq = _like_count_sq()
    if sort == "likes":
        stmt = stmt.order_by(
            likes_sq.desc(), DailyFeedEntry.feed_date.desc(), DailyFeedEntry.created_at.desc()
        )
    else:  # date
        stmt = stmt.order_by(DailyFeedEntry.feed_date.desc(), DailyFeedEntry.created_at.desc())
    stmt = stmt.offset((page - 1) * size).limit(size)

    rows = (await session.execute(stmt)).all()
    likes = await _likes_by_entry(session, [entry.id for entry, _ in rows], user_id=user_id)
    empty = {"like_count": 0, "liked_by_me": False, "likers_preview": []}
    return [_entry_item(entry, paper, likes.get(entry.id, empty)) for entry, paper in rows], total


async def get_entry_item(
    session: AsyncSession, *, entry_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, Any]:
    entry = await session.get(DailyFeedEntry, entry_id)
    if entry is None:
        raise DailyEntryNotFoundError(str(entry_id))
    paper = await session.get(Paper, entry.paper_id)
    assert paper is not None  # 外键保证
    likes = await _likes_by_entry(session, [entry.id], user_id=user_id)
    item = _entry_item(entry, paper, likes[entry.id])
    item["wiki_content"] = entry.wiki_content
    return item


# ---- 点赞 ----


async def set_like(
    session: AsyncSession, *, entry_id: uuid.UUID, user_id: uuid.UUID, liked: bool
) -> dict[str, Any]:
    """点/取消赞，幂等；返回该 entry 最新点赞汇总（乐观更新对账用）。"""
    entry = await session.get(DailyFeedEntry, entry_id)
    if entry is None:
        raise DailyEntryNotFoundError(str(entry_id))
    existing = (
        await session.execute(
            select(DailyFeedLike).where(
                DailyFeedLike.entry_id == entry_id, DailyFeedLike.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    if liked and existing is None:
        session.add(DailyFeedLike(entry_id=entry_id, user_id=user_id))
    elif not liked and existing is not None:
        await session.delete(existing)
    await session.commit()
    likes = await _likes_by_entry(session, [entry_id], user_id=user_id)
    return {"entry_id": entry_id, **likes[entry_id]}


async def list_likers(session: AsyncSession, *, entry_id: uuid.UUID) -> list[dict[str, Any]]:
    entry = await session.get(DailyFeedEntry, entry_id)
    if entry is None:
        raise DailyEntryNotFoundError(str(entry_id))
    rows = (
        await session.execute(
            select(User.id, User.display_name, User.avatar_path, DailyFeedLike.created_at)
            .join(DailyFeedLike, DailyFeedLike.user_id == User.id)
            .where(DailyFeedLike.entry_id == entry_id)
            .order_by(DailyFeedLike.created_at.desc())
        )
    ).all()
    return [
        {"id": uid, "display_name": name, "has_avatar": bool(avatar), "liked_at": at}
        for uid, name, avatar, at in rows
    ]


async def list_my_liked(
    session: AsyncSession, *, user_id: uuid.UUID, page: int = 1, size: int = 20
) -> tuple[list[dict[str, Any]], int]:
    """我赞过的（个人库历史 tab）：按点赞时间倒序，随 entry 过期自然消失。"""
    base = (
        select(DailyFeedEntry, Paper, DailyFeedLike.created_at)
        .join(DailyFeedLike, DailyFeedLike.entry_id == DailyFeedEntry.id)
        .join(Paper, Paper.id == DailyFeedEntry.paper_id)
        .where(DailyFeedLike.user_id == user_id)
    )
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await session.execute(
            base.order_by(DailyFeedLike.created_at.desc()).offset((page - 1) * size).limit(size)
        )
    ).all()
    likes = await _likes_by_entry(session, [entry.id for entry, _, _ in rows], user_id=user_id)
    empty = {"like_count": 0, "liked_by_me": False, "likers_preview": []}
    items = []
    for entry, paper, liked_at in rows:
        item = _entry_item(entry, paper, likes.get(entry.id, empty))
        item["liked_at"] = liked_at
        items.append(item)
    return items, total


# ---- 收录到各类库 ----


async def entry_collections(
    session: AsyncSession, *, entry_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, Any]:
    """该论文已在哪些收录目标里（树选框预勾选/禁用用）。"""
    entry = await session.get(DailyFeedEntry, entry_id)
    if entry is None:
        raise DailyEntryNotFoundError(str(entry_id))
    library_ids = (
        (
            await session.execute(
                select(LibraryPaper.library_id).where(LibraryPaper.paper_id == entry.paper_id)
            )
        )
        .scalars()
        .all()
    )
    topic_ids = (
        (
            await session.execute(
                select(TopicPaper.topic_id).where(TopicPaper.paper_id == entry.paper_id)
            )
        )
        .scalars()
        .all()
    )
    paper = await session.get(Paper, entry.paper_id)
    assert paper is not None
    personal_entry = await user_library.entry_for_paper(session, user_id=user_id, paper=paper)
    return {
        "direction_library_ids": list(library_ids),
        "topic_ids": list(topic_ids),
        "in_personal": bool(personal_entry is not None and personal_entry.saved),
    }


async def collect_papers(
    session: AsyncSession,
    *,
    user: User,
    paper_ids: list[uuid.UUID],
    direction_library_ids: list[uuid.UUID],
    topic_ids: list[uuid.UUID],
    personal: bool = False,
) -> list[dict[str, Any]]:
    """把一批论文分发进方向库 / 课题书架 / 个人库；逐目标返回结果，无权只标记不失败。"""
    papers = [p for pid in paper_ids if (p := await session.get(Paper, pid)) is not None]
    results: list[dict[str, Any]] = []

    for library_id in direction_library_ids:
        library = await session.get(DirectionLibrary, library_id)
        if library is None or not await can_manage_library(session, user=user, library=library):
            results.append(
                {
                    "target_type": "library",
                    "target_id": library_id,
                    "added": 0,
                    "skipped_existing": 0,
                    "forbidden": True,
                }
            )
            continue
        added = skipped = 0
        for paper in papers:
            _, created = await ensure_membership(
                session, library_id=library_id, paper_id=paper.id, status="included"
            )
            added += int(created)
            skipped += int(not created)
        await session.commit()
        results.append(
            {
                "target_type": "library",
                "target_id": library_id,
                "added": added,
                "skipped_existing": skipped,
                "forbidden": False,
            }
        )

    for topic_id in topic_ids:
        project = await projects_service.get_project(session, project_id=topic_id, user_id=user.id)
        if project is None:
            results.append(
                {
                    "target_type": "topic",
                    "target_id": topic_id,
                    "added": 0,
                    "skipped_existing": 0,
                    "forbidden": True,
                }
            )
            continue
        added = skipped = 0
        for paper in papers:
            existing = (
                await session.execute(
                    select(TopicPaper.id).where(
                        TopicPaper.topic_id == topic_id, TopicPaper.paper_id == paper.id
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                skipped += 1
                continue
            await shelf_service.add_to_shelf(
                session, project_id=topic_id, paper_id=paper.id, user_id=user.id
            )
            added += 1
        results.append(
            {
                "target_type": "topic",
                "target_id": topic_id,
                "added": added,
                "skipped_existing": skipped,
                "forbidden": False,
            }
        )

    if personal:
        added = skipped = 0
        for paper in papers:
            existing = await user_library.entry_for_paper(session, user_id=user.id, paper=paper)
            if existing is not None and existing.saved:
                skipped += 1
                continue
            await user_library.save_paper(session, user_id=user.id, paper=paper)
            added += 1
        results.append(
            {
                "target_type": "personal",
                "target_id": None,
                "added": added,
                "skipped_existing": skipped,
                "forbidden": False,
            }
        )

    return results
