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

import asyncio
import datetime as dt
import json
import logging
import re
import uuid
from typing import Any

from sqlalchemy import String, case, cast, delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding_space import EmbeddingSpace, active_space
from app.models.daily_feed import (
    DEFAULT_DAILY_CATEGORIES,
    DailyFeedEntry,
    DailyFeedLike,
)
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import Paper, PaperWiki, new_paper
from app.models.system_setting import SystemSetting
from app.models.topic_shelf import TopicPaper
from app.models.user import User
from app.models.vectors import PaperVector
from app.models.voyage import TERMINAL_STATUSES, VoyageRun, VoyageStep
from app.services import paper_wiki, user_library
from app.services import projects as projects_service
from app.services import topic_shelf as shelf_service
from app.services.dedup import pool_dedup_key
from app.services.libraries import can_manage_library, ensure_membership, find_pool_paper
from app.services.literature import get_arxiv_client
from app.services.paper_import import _parse_iso

logger = logging.getLogger(__name__)

CATEGORIES_SETTING_KEY = "daily_feed_categories"

# 注：论文级向量的管理员开关已升格为平台总闸并改名（daily_feed_embed_enabled →
# 默认关意味着每日推送的论文连论文级向量都没有、语义检索里根本搜不到；而只管每日推送
# 这一条路径也名不副实。本模块只管调用总闸，不再自己存开关。旧键的存量值不再读。

# 批量嵌入的每批条数（与 chunks.EMBED_BATCH 对齐）
EMBED_BATCH = 32

# arXiv 分类形如 cs.AI / stat.ML / eess.IV / math.OC（主类小写，子类大写字母数字短串）
_CATEGORY_RE = re.compile(r"^[a-z][a-z-]{1,15}(\.[A-Za-z]{2,10})?$")

_MAX_LIKERS_PREVIEW = 5


class DailyEntryNotFoundError(Exception):
    pass


class CompileInProgressError(Exception):
    """同一 entry 的解读编译已在进行中（进程内防抖）。"""


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


# ---- 池论文向量（语义检索/池对话底座） ----


async def embed_papers_missing_vectors(
    session: AsyncSession,
    *,
    paper_ids: list[uuid.UUID],
    user_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """给定论文中还没有向量的批量嵌入（幂等：已有向量的一律不动）。

    「已有向量」按**激活空间**算：换了嵌入模型之后这些论文都算缺向量，会重新建。
    best-effort：整批失败只记日志并继续下一批；provider 不支持嵌入直接停手。
    返回 {embedded, skipped(已有向量), failed(嵌入未成功)}。
    """
    if not paper_ids:
        return {"embedded": 0, "skipped": 0, "failed": 0}
    from app.services.embedding import (
        embed_documents,
        papers_with_vector,
        upsert_paper_vector,
    )
    from app.services.paper_enrich import paper_embedding_text

    rows = (
        (await session.execute(select(Paper).where(Paper.id.in_(list(set(paper_ids))))))
        .scalars()
        .all()
    )
    space = await active_space(session)
    done = (
        await papers_with_vector(session, [p.id for p in rows], space)
        if space is not None
        else set()
    )
    pending = [p for p in rows if p.id not in done]
    stats = {"embedded": 0, "skipped": len(rows) - len(pending), "failed": 0}
    if not pending:
        return stats
    # 先把文本与 id 取出来：失败回滚会让 ORM 实例过期，之后再读属性会触发意外 IO
    items = [(p.id, paper_embedding_text(p)) for p in pending]
    for i in range(0, len(items), EMBED_BATCH):
        batch = items[i : i + EMBED_BATCH]
        try:
            vectors, batch_space = await embed_documents(
                session, [t for _, t in batch], user_id=user_id
            )
        except asyncio.CancelledError:
            raise
        except NotImplementedError:
            logger.info("daily embed skipped: provider does not support embeddings")
            stats["failed"] += len(items) - i
            break
        except Exception:  # noqa: BLE001 — 嵌入失败不阻断同步
            logger.warning("daily embed batch failed", exc_info=True)
            stats["failed"] += len(batch)
            continue
        try:
            for (paper_id, _), vector in zip(batch, vectors, strict=True):
                await upsert_paper_vector(session, paper_id, vector, batch_space)
            await session.commit()
            stats["embedded"] += len(batch)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("daily embed commit failed", exc_info=True)
            await session.rollback()
            stats["failed"] += len(batch)
    return stats


async def backfill_embeddings(
    session: AsyncSession, *, user_id: uuid.UUID | None = None, today: dt.date | None = None
) -> dict[str, int]:
    """给当前 7 天窗口内所有缺向量的每日论文补建向量（管理员开开关后一次性补齐用）。"""
    cutoff = (today or _today_utc()) - dt.timedelta(days=await get_retention_days(session) - 1)
    paper_ids = list(
        (
            await session.execute(
                select(DailyFeedEntry.paper_id).where(DailyFeedEntry.feed_date >= cutoff)
            )
        )
        .scalars()
        .all()
    )
    return await embed_papers_missing_vectors(session, paper_ids=paper_ids, user_id=user_id)


async def embedding_coverage(
    session: AsyncSession, *, today: dt.date | None = None
) -> tuple[int, int]:
    """(有向量的池论文数, 池论文总数)——语义检索结果覆盖度提示用。

    「有向量」按**激活空间**算：刚换过嵌入模型时覆盖率会如实掉下来，前端照此提示，
    而不是拿旧空间的向量数充数。
    """
    cutoff = (today or _today_utc()) - dt.timedelta(days=await get_retention_days(session) - 1)
    base = (
        select(func.count())
        .select_from(DailyFeedEntry)
        .join(Paper, Paper.id == DailyFeedEntry.paper_id)
        .where(DailyFeedEntry.feed_date >= cutoff)
    )
    total = (await session.execute(base)).scalar_one()
    space = await active_space(session)
    if space is None:
        return 0, int(total)
    ready = (
        await session.execute(
            base.join(PaperVector, PaperVector.paper_id == Paper.id).where(
                PaperVector.space == space.key
            )
        )
    ).scalar_one()
    return int(ready), int(total)


# ---- 每日同步（cron / 手动刷新） ----


def _make_pool_paper(entry: dict[str, Any]) -> Paper:
    """RSS entry → 轻量内容池 Paper（不下 PDF、不补机构——feed 量大，重活留给收录后）。"""
    aid = entry.get("arxiv_id")
    return new_paper(
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
    """删过期 entry（保留含今天共 RETENTION 天）；likes 显式跟删（不依赖 DB 级联）。

    过期退出推送后，若某文章不再被任何集合引用（库/书架/个人库/论著），回收其内容池
    本体 + 落盘文件——否则从没人收藏的每日推送会把内容池越堆越大。
    """
    cutoff = (today or _today_utc()) - dt.timedelta(days=await get_retention_days(session) - 1)
    expired = (
        await session.execute(
            select(DailyFeedEntry.id, DailyFeedEntry.paper_id).where(
                DailyFeedEntry.feed_date < cutoff
            )
        )
    ).all()
    if not expired:
        return 0
    expired_ids = [row.id for row in expired]
    await session.execute(delete(DailyFeedLike).where(DailyFeedLike.entry_id.in_(expired_ids)))
    await session.execute(delete(DailyFeedEntry).where(DailyFeedEntry.id.in_(expired_ids)))
    # 延迟 import 避免与 papers 服务循环依赖；entry 已删，孤儿检查不会再命中本推送
    from app.services.papers import gc_orphan_papers

    await gc_orphan_papers(session, [row.paper_id for row in expired])
    return len(expired_ids)


async def fetch_new_by_category(
    session: AsyncSession,
) -> tuple[list[str], dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    """抓订阅分类的当天新公告；返回 (分类列表, {分类: 条目}, {分类: 状态})。

    **逐个分类记录成败**：一个分类抓失败不影响其余分类，但必须如实报出来。以前的
    做法是让客户端把异常吞成 []，于是「cs.AI 被限流」和「cs.AI 今天没有新论文」在
    上层完全无法区分——只有全部分类都空时才会报错，部分失败则悄悄丢掉那一天那个
    分类的全部论文。每日池是所有文献库的唯一供给，这种丢失是补不回来的。

    状态取值：``ok``（抓到了，可能是 0 篇——周末/无公告是正常的）、``error``。
    """
    categories = await get_categories(session)
    client = get_arxiv_client()
    by_category: dict[str, list[dict[str, Any]]] = {}
    statuses: dict[str, dict[str, Any]] = {}
    for category in categories:
        try:
            entries, batch_at = await client.fetch_new(category)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — 单分类失败不打断其余分类
            logger.warning("daily feed RSS failed for %s", category, exc_info=True)
            by_category[category] = []
            statuses[category] = {
                "count": 0,
                "status": "error",
                "detail": f"{type(e).__name__}: {e}"[:200],
            }
            continue
        by_category[category] = entries
        batch_date = batch_at.astimezone(dt.UTC).date() if batch_at else None
        statuses[category] = {
            "count": len(entries),
            "status": "ok",
            "detail": None,
            # arXiv 自己声明这批是哪天的公告；调用方据此判断有没有抓早了
            "batch_date": batch_date.isoformat() if batch_date else None,
            "stale": bool(batch_date and batch_date < _today_utc()),
        }
    return categories, by_category, statuses


async def upsert_entries(
    session: AsyncSession,
    *,
    by_category: dict[str, list[dict[str, Any]]],
    today: dt.date | None = None,
) -> dict[str, Any]:
    """RSS 条目 → 全局内容池去重 + 建/合并推送 entry；返回 {created, merged, touched}。

    同一篇多分类命中（cross-list）合并进 categories、不重复建行；paper_id 唯一约束
    保证同日重跑幂等（created=0）。``touched`` 是本次涉及的池论文 id（补向量用）。
    """
    feed_date = today or _today_utc()
    created = merged = 0
    touched: list[uuid.UUID] = []

    for category, entries in by_category.items():
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
            touched.append(paper.id)
            row = (
                await session.execute(
                    select(DailyFeedEntry).where(DailyFeedEntry.paper_id == paper.id)
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    DailyFeedEntry(
                        paper_id=paper.id,
                        feed_date=feed_date,
                        primary_category=category,
                        categories=[category],
                        announce_type=entry.get("announce_type") or "new",
                    )
                )
                created += 1
            elif category not in (row.categories or []):
                # 同日另一分类命中（cross-list）：合并分类，不动 feed_date
                row.categories = [*(row.categories or []), category]
                merged += 1

    await session.commit()
    return {"created": created, "merged": merged, "touched": touched}


async def embed_touched_papers(
    session: AsyncSession,
    *,
    paper_ids: list[uuid.UUID],
    user_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """给本次涉及的池论文建分段兜底块 + 论文级向量；返回 {enabled, embedded, failed, chunked}。

    每日推送的论文抓取时不下 PDF，一篇全文都没有，过去又受一个默认关着的管理员开关
    管着，于是这些论文既没有分段也没有论文级向量——对文献对话完全不存在。现在无条件
    建（``enabled`` 恒为 True，保留字段是为了不动调用方与既有 observation 口径）。

    best-effort：向量化本来就是可选增强，失败只记日志并把原因放进 ``embed_error``
    （不放 ``error``——那会让任务这一步判失败）。
    """
    try:
        chunked = await ensure_chunks_for_papers(session, paper_ids=paper_ids)
        stats = await embed_papers_missing_vectors(
            session, paper_ids=paper_ids, user_id=user_id
        )
        # 兜底块的向量 = 刚建好的论文级向量的拷贝（零 token），故排在嵌入之后
        from app.services.chunks import sync_abstract_chunk_vectors

        await sync_abstract_chunk_vectors(session, paper_ids=paper_ids)
        await session.commit()
        return {
            "enabled": True,
            "embedded": stats["embedded"],
            "failed": stats["failed"],
            "chunked": chunked,
        }
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.warning("daily feed embedding step failed", exc_info=True)
        return {"enabled": True, "embedded": 0, "failed": 0, "embed_error": str(e)}


async def ensure_chunks_for_papers(
    session: AsyncSession, *, paper_ids: list[uuid.UUID]
) -> int:
    """给这批论文补可检索分段（没有全文→标题+摘要单块），返回新建了分段的篇数。

    每日推送不下 PDF，这里建出来的基本都是摘要兜底块；日后真抓了 PDF，
    ``ensure_paper_chunks`` 会用全文块把它整体替换掉。
    """
    if not paper_ids:
        return 0
    from app.services.chunks import ensure_paper_chunks

    rows = (
        (await session.execute(select(Paper).where(Paper.id.in_(list(set(paper_ids))))))
        .scalars()
        .all()
    )
    chunked = 0
    for paper in rows:
        try:
            if await ensure_paper_chunks(session, paper):
                chunked += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 单篇分段失败不打断整批
            logger.warning("daily chunk indexing failed for paper %s", paper.id, exc_info=True)
            await session.rollback()
    await session.commit()
    return chunked


async def sync_daily_feed(session: AsyncSession) -> dict[str, Any]:
    """抓订阅分类的当天新公告入池 + 清理过期；幂等（同日重跑 created=0）。

    直连入口（不建任务），供脚本/测试一把梭；线上走任务系统的 daily.* 四步动作
    （app/agents/voyage/actions_daily.py），两条路径共用下面这几个步骤函数。
    """
    today = _today_utc()
    categories, by_category, _statuses = await fetch_new_by_category(session)
    fetched = sum(len(v) for v in by_category.values())
    stats = await upsert_entries(session, by_category=by_category, today=today)
    expired = await cleanup_expired(session, today=today)
    await session.commit()
    embed = await embed_touched_papers(session, paper_ids=stats["touched"])
    return {
        "fetched": fetched,
        "created": stats["created"],
        "expired": expired,
        "categories": categories,
        "embedded": embed["embedded"],
    }


# ---- 每日同步任务（voyage kind=daily_feed_sync） ----

DAILY_FEED_VOYAGE_KIND = "daily_feed_sync"


class DailyFeedConflictError(Exception):
    """已有一个每日新论文抓取任务在跑（全局单例，无库/课题维度）。"""


async def find_running_daily_feed_voyage(session: AsyncSession) -> VoyageRun | None:
    """在跑的每日新论文抓取任务（全局唯一）；没有返回 None。"""
    stmt = (
        select(VoyageRun)
        .where(
            VoyageRun.kind == DAILY_FEED_VOYAGE_KIND,
            VoyageRun.status.not_in(tuple(TERMINAL_STATUSES)),
        )
        .order_by(VoyageRun.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_daily_feed_voyage(
    session: AsyncSession, *, created_by: uuid.UUID | None
) -> VoyageRun:
    """建一次「每日新论文抓取」任务（互斥检查），由调用方入队 run_voyage。

    这个任务既不属于课题也不属于库（全实验室共享的每日推送），两个作用域 id 都为空；
    可见性口径与订阅分类管理/手动刷新一致——仅平台管理员（services/voyages.py）。
    只有最后一步「建立语义向量」可能花 token 且量很小，故不设 token 预算。
    """
    if await find_running_daily_feed_voyage(session) is not None:
        raise DailyFeedConflictError(DAILY_FEED_VOYAGE_KIND)
    run = VoyageRun(
        kind=DAILY_FEED_VOYAGE_KIND,
        goal=f"每日新论文抓取（{_today_utc().isoformat()}）",
        status="planning",
        cursor=0,
        budget={"max_tokens": None},
        created_by=created_by,
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


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
        "affiliations": paper.affiliations or [],
        "abstract": paper.abstract,
        "year": paper.year,
        "arxiv_id": paper.arxiv_id,
        "url": paper.url,
        "published_at": paper.published_at,
        "has_wiki": paper.wiki is not None,
        **likes,
    }


async def entry_items(
    session: AsyncSession,
    rows: list[tuple[DailyFeedEntry, Paper]],
    *,
    user_id: uuid.UUID,
) -> list[dict[str, Any]]:
    """(entry, paper) 行 → 列表项（补点赞汇总）；保持传入顺序。"""
    likes = await _likes_by_entry(session, [entry.id for entry, _ in rows], user_id=user_id)
    empty = {"like_count": 0, "liked_by_me": False, "likers_preview": []}
    return [_entry_item(entry, paper, likes.get(entry.id, empty)) for entry, paper in rows]


async def list_days(
    session: AsyncSession,
    *,
    announce: str | None = None,
    category: str | None = None,
    collected: bool = False,
) -> list[dict[str, Any]]:
    """每天的条目数。**按当前筛选算**——日期标签上的数字与列表里看到的必须是同一回事，
    否则选了 cs.CV 之后标签仍显示全部篇数，看起来就像筛选没生效。

    筛选条件与 :func:`list_papers` 保持同一口径。
    """
    stmt = select(DailyFeedEntry.feed_date, func.count(DailyFeedEntry.id))
    if collected:
        from app.models.library_direction import LibraryPaper
        from app.services.papers import PAPER_STATUS_GROUPS

        stmt = stmt.where(
            DailyFeedEntry.paper_id.in_(
                select(LibraryPaper.paper_id).where(
                    LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"])
                )
            )
        )
    if announce in ("new", "cross"):
        stmt = stmt.where(DailyFeedEntry.announce_type == announce)
    if category:
        stmt = stmt.where(
            (DailyFeedEntry.primary_category == category)
            | cast(DailyFeedEntry.categories, String).like(f'%"{category}"%')
        )
    rows = (
        await session.execute(
            stmt.group_by(DailyFeedEntry.feed_date).order_by(DailyFeedEntry.feed_date.desc())
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
    announce: str | None = None,
    category: str | None = None,
    author: str | None = None,
    affiliation: str | None = None,
    library_id: uuid.UUID | None = None,
    collected: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    stmt = select(DailyFeedEntry, Paper).join(Paper, Paper.id == DailyFeedEntry.paper_id)
    if collected:
        # 「已收录」：被**任意**文献库真正收进去的（候选/回收站不算）。与 library_id
        # 的区别是不限定哪个库——它是每日页的默认视角：只看进了库的。
        from app.models.library_direction import LibraryPaper
        from app.services.papers import PAPER_STATUS_GROUPS

        stmt = stmt.where(
            Paper.id.in_(
                select(LibraryPaper.paper_id).where(
                    LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"])
                )
            )
        )
    if date is not None:
        stmt = stmt.where(DailyFeedEntry.feed_date == date)
    if library_id is not None:
        # 「这篇被哪个库收进去了」：只算真正收录的成员行，候选和回收站不算
        from app.models.library_direction import LibraryPaper
        from app.services.papers import PAPER_STATUS_GROUPS

        stmt = stmt.where(
            Paper.id.in_(
                select(LibraryPaper.paper_id).where(
                    LibraryPaper.library_id == library_id,
                    LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
                )
            )
        )

    if q:
        stmt = stmt.where(Paper.title.ilike(f"%{q.strip()}%"))
    # 作者 / 机构：在 JSON 列上做文本包含匹配（同 services/papers.apply_paper_filters 口径）
    if author:
        stmt = stmt.where(cast(Paper.authors, String).ilike(f"%{author}%"))
    if affiliation:
        stmt = stmt.where(cast(Paper.affiliations, String).ilike(f"%{affiliation}%"))
    if announce in ("new", "cross"):
        stmt = stmt.where(DailyFeedEntry.announce_type == announce)
    if category:
        # 命中任一分类（categories 是 JSON 数组；LIKE 序列化文本，PG/sqlite 通用）
        stmt = stmt.where(
            (DailyFeedEntry.primary_category == category)
            | cast(DailyFeedEntry.categories, String).like(f'%"{category}"%')
        )

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = (await session.execute(count_stmt)).scalar_one()

    likes_sq = _like_count_sq()
    # 分类优先级打头：cs.CL 的排最前，cs.RO 的排最后（一篇同时挂两者按 cs.CL 算）。
    # LIKE 序列化文本的写法与上面 category 过滤同口径，PG/sqlite 通用。
    category_rank = case(
        (
            (DailyFeedEntry.primary_category == "cs.CL")
            | cast(DailyFeedEntry.categories, String).like('%"cs.CL"%'),
            0,
        ),
        (
            (DailyFeedEntry.primary_category == "cs.RO")
            | cast(DailyFeedEntry.categories, String).like('%"cs.RO"%'),
            2,
        ),
        else_=1,
    )
    # 同分类里，新工作排在交叉提交（更新）前面：新工作才是当天真正的新东西
    announce_rank = case((DailyFeedEntry.announce_type == "new", 0), else_=1)
    if sort == "likes":
        stmt = stmt.order_by(
            category_rank,
            announce_rank,
            likes_sq.desc(),
            DailyFeedEntry.feed_date.desc(),
            DailyFeedEntry.created_at.desc(),
        )
    else:  # date
        stmt = stmt.order_by(
            category_rank,
            announce_rank,
            DailyFeedEntry.feed_date.desc(),
            DailyFeedEntry.created_at.desc(),
        )
    stmt = stmt.offset((page - 1) * size).limit(size)

    rows = (await session.execute(stmt)).all()
    return await entry_items(session, list(rows), user_id=user_id), total


async def semantic_search_daily(
    session: AsyncSession,
    *,
    query_vector: list[float],
    space: EmbeddingSpace,
    limit: int,
    date: dt.date | None = None,
    category: str | None = None,
    announce: str | None = None,
    author: str | None = None,
    affiliation: str | None = None,
    library_id: uuid.UUID | None = None,
    collected: bool = False,
) -> list[tuple[DailyFeedEntry, Paper, float]]:
    """池内向量检索（pgvector 余弦；仅 postgres，调用方先判 semantic_search_supported）。

    只召回**在给定空间下**已有向量的池论文，所以结果可能不全，调用方需要如实告知
    前端。筛选条件与关键词列表一致（日期/分类/公告类型/作者/机构）。
    """
    where = ["v.space = :space"]
    params: dict[str, Any] = {
        "qv": json.dumps(query_vector),
        "k": limit,
        "space": space.key,
    }
    if date is not None:
        where.append("e.feed_date = :feed_date")
        params["feed_date"] = date
    if announce in ("new", "cross"):
        where.append("e.announce_type = :announce")
        params["announce"] = announce
    if category:
        where.append(
            "(e.primary_category = :category OR CAST(e.categories AS text) LIKE :category_like)"
        )
        params["category"] = category
        params["category_like"] = f'%"{category}"%'
    if author:
        where.append("CAST(p.authors AS text) ILIKE :author_like")
        params["author_like"] = f"%{author}%"
    if affiliation:
        where.append("CAST(p.affiliations AS text) ILIKE :affiliation_like")
        params["affiliation_like"] = f"%{affiliation}%"
    if library_id is not None:
        from app.services.papers import PAPER_STATUS_GROUPS

        where.append(
            "EXISTS (SELECT 1 FROM library_papers lp WHERE lp.paper_id = p.id "
            "AND lp.library_id = :library_id "
            "AND lp.status = ANY(CAST(:lib_statuses AS varchar[])))"
        )
        params["library_id"] = str(library_id)
        params["lib_statuses"] = list(PAPER_STATUS_GROUPS["library"])
    if collected:
        from app.services.papers import PAPER_STATUS_GROUPS

        where.append(
            "EXISTS (SELECT 1 FROM library_papers lpc WHERE lpc.paper_id = p.id "
            "AND lpc.status = ANY(CAST(:collected_statuses AS varchar[])))"
        )
        params["collected_statuses"] = list(PAPER_STATUS_GROUPS["library"])
    rows = (
        await session.execute(
            text(
                "SELECT e.id AS entry_id, 1 - (v.embedding <=> CAST(:qv AS vector)) AS score "
                "FROM daily_feed_entries e "
                "JOIN papers p ON p.id = e.paper_id "
                "JOIN paper_vectors v ON v.paper_id = p.id "
                f"WHERE {' AND '.join(where)} "
                "ORDER BY score DESC "
                "LIMIT :k"
            ),
            params,
        )
    ).all()
    if not rows:
        return []
    scores = {row.entry_id: float(row.score) for row in rows}
    pairs = (
        await session.execute(
            select(DailyFeedEntry, Paper)
            .join(Paper, Paper.id == DailyFeedEntry.paper_id)
            .where(DailyFeedEntry.id.in_(list(scores)))
        )
    ).all()
    by_id = {entry.id: (entry, paper) for entry, paper in pairs}
    return [(*by_id[eid], scores[eid]) for eid in (row.entry_id for row in rows) if eid in by_id]


async def get_entry_item(
    session: AsyncSession, *, entry_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, Any]:
    entry = await session.get(DailyFeedEntry, entry_id)
    if entry is None:
        raise DailyEntryNotFoundError(str(entry_id))
    from sqlalchemy.orm import selectinload

    # 概念随论文一起取（详情才需要，列表不带）
    paper = await session.get(Paper, entry.paper_id, options=[selectinload(Paper.concepts)])
    assert paper is not None  # 外键保证
    likes = await _likes_by_entry(session, [entry.id], user_id=user_id)
    item = _entry_item(entry, paper, likes[entry.id])
    # 解读走论文级唯一那份（paper_wikis）：库里编译过的这里直接能看到，反之亦然
    item["wiki_content"] = paper.wiki_content
    item["pdf_available"] = paper.pdf_available
    item["wiki_model"] = paper.wiki.model if paper.wiki is not None else None
    item["compiled_at"] = paper.wiki.updated_at if paper.wiki is not None else None
    # 编译者显示名：重新编译会覆盖，前端据此提示（人被删 / 存量数据留空）
    names = await paper_wiki.compiler_names(
        session, [paper.wiki.compiled_by if paper.wiki is not None else None]
    )
    compiled_by = paper.wiki.compiled_by if paper.wiki is not None else None
    item["compiled_by_name"] = names.get(compiled_by) if compiled_by else None
    item["concepts"] = [
        {"id": c.id, "name": c.name, "category": c.category} for c in (paper.concepts or [])
    ]
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


# ---- 池对话与单篇解读（P2） ----


async def daily_paper_ids(session: AsyncSession) -> list[uuid.UUID]:
    """池内现存全部论文 id（池对话的 scope）。"""
    return list((await session.execute(select(DailyFeedEntry.paper_id))).scalars().all())


# 进行中的解读编译（entry_id）；单实例进程内防抖，多副本最坏重复编译一次、后写覆盖
_COMPILING: set[uuid.UUID] = set()


async def fetch_entry_pdf(
    session: AsyncSession, *, entry_id: uuid.UUID, user_id: uuid.UUID
) -> Paper:
    """给某条每日论文补下 PDF + 抽全文（幂等：已有 PDF 直接返回）。

    每日池是全实验室共享的，且池论文通常不属于任何库/书架，走不了
    ``/papers/{id}/fetch-pdf`` 的成员可见性兜底——故按 entry 授权单开一条。
    """
    from app.services.papers import fetch_pdf

    entry = await session.get(DailyFeedEntry, entry_id)
    if entry is None:
        raise DailyEntryNotFoundError(str(entry_id))
    paper = await session.get(Paper, entry.paper_id)
    assert paper is not None  # 外键保证
    return await fetch_pdf(session, paper, user_id=user_id)


async def compile_entry_wiki(
    session: AsyncSession, *, entry_id: uuid.UUID, user_id: uuid.UUID
) -> PaperWiki:
    """编译这篇论文的解读并写进 paper_wikis（全平台一份）；费用记个人。

    任何人都能编译，已编译过的也能再编译——覆盖同一行，以最新一次为准
    （compiled_by 一并更新）；只有「同一篇正在编译中」才拒（CompileInProgressError）。
    每日池论文建池时不下 PDF，直接编译只能拿摘要产出纯文字稿；故先尽力补下 PDF
    （幂等，失败则降级），再抽图 + 标注重要图，与单篇重新编译同款逻辑产出图文解读。
    编译完就地上链概念（与重新编译同款）：概念与库无关，池里的论文不属于任何库也照建，
    否则解读里的 [[双链]] 全点不开。
    """
    from pathlib import Path

    from app.core.llm.router import get_llm_router
    from app.services.concepts import link_paper_concepts
    from app.services.figure_annotate import annotate_figures
    from app.services.literature.pdf_extract import extract_figures
    from app.services.paper_wiki import upsert_wiki
    from app.services.wiki_compile import compile_paper

    entry = await session.get(DailyFeedEntry, entry_id)
    if entry is None:
        raise DailyEntryNotFoundError(str(entry_id))
    paper = await session.get(Paper, entry.paper_id)
    assert paper is not None  # 外键保证
    if entry_id in _COMPILING:
        raise CompileInProgressError(str(entry_id))
    _COMPILING.add(entry_id)
    try:
        # 没 PDF 就先抓一次（顺带抽全文/分块）：图文解读的前提。best-effort——
        # 无 arxiv_id / 下载失败时照常往下走，产出纯文字稿而不是整体失败。
        if not paper.pdf_path:
            from app.services.papers import (
                PdfFetchFailedError,
                PdfSourceUnsupportedError,
                fetch_pdf,
            )

            try:
                paper = await fetch_pdf(session, paper, user_id=user_id)
            except (PdfSourceUnsupportedError, PdfFetchFailedError):
                logger.info("daily compile: no PDF for paper %s, text-only", paper.id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — 抓取异常不阻断编译
                logger.warning("daily compile: fetch_pdf failed for %s", paper.id, exc_info=True)
        if paper.pdf_path and Path(paper.pdf_path).exists():
            # 从未提取过，或上一轮一张重要图都没选出来 → 重提候选（对齐 recompile_paper）
            if paper.figures is None or not any(f.get("important") for f in paper.figures):
                candidates = await extract_figures(str(paper.id), Path(paper.pdf_path))
                paper.figures = [
                    c | {"caption": None, "kind": None, "important": False} for c in candidates
                ]
            if paper.figures:
                await annotate_figures(paper, paper.figures, user_id=user_id)
            await session.commit()
        compiled = await compile_paper(paper, user_id=user_id)
    finally:
        _COMPILING.discard(entry_id)
    wiki = await upsert_wiki(
        session,
        paper=paper,
        content=compiled.content,
        model=compiled.model or None,
        compiled_by=user_id,
    )
    await session.commit()
    # 单篇概念上链：正文里的 [[双链]] 建词条并关联。不传成员行 → 记账落平台级
    # （library_id 空）+ 触发编译的人，不把费用摊给某个不相干的库。
    await link_paper_concepts(session, paper, llm=get_llm_router(), user_id=user_id)
    return wiki


# ---- 收录到各类库 ----


#: 手动收录时可以被「提升」为 included 的既有状态。已经在库内的（scored/fetched/
#: compiled/included）保持原样，不该被一次手动收录降级或重置。
PROMOTABLE_ON_MANUAL_COLLECT = ("candidate", "excluded")


async def entry_collections(
    session: AsyncSession, *, entry_id: uuid.UUID, user_id: uuid.UUID
) -> dict[str, Any]:
    """该论文已在哪些收录目标里（树选框预勾选/禁用用）。"""
    entry = await session.get(DailyFeedEntry, entry_id)
    if entry is None:
        raise DailyEntryNotFoundError(str(entry_id))
    # 只算**真正进了库**的状态，与 papers.collecting_libraries 和列表口径一致。
    # 以前不过滤，于是刚被粗排选中、还没打分的 candidate 也显示成「已在库中」，
    # 复选框还被禁用——想手动收进去都点不了。excluded（回收站）同理。
    from app.services.papers import PAPER_STATUS_GROUPS

    library_ids = (
        (
            await session.execute(
                select(LibraryPaper.library_id).where(
                    LibraryPaper.paper_id == entry.paper_id,
                    LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
                )
            )
        )
        .scalars()
        .all()
    )
    topic_ids = (
        (
            await session.execute(
                select(TopicPaper.topic_id).where(
                    TopicPaper.paper_id == entry.paper_id, TopicPaper.trashed_at.is_(None)
                )
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
    """把一批论文分发进方向库 / 课题书架 / 个人库；逐目标返回结果，无权只标记不失败。

    解读不用跟着搬：每篇论文一份存 paper_wikis，entry 7 天过期后照样读得到。
    """
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
            membership, created = await ensure_membership(
                session, library_id=library_id, paper_id=paper.id, status="included"
            )
            if created:
                added += 1
            elif membership.status in PROMOTABLE_ON_MANUAL_COLLECT:
                # 人已经明确说「收进这个库」，那就照做：候选（还没轮到打分）和
                # 回收站里的（自动淘汰过）都提升为人工纳入，而不是静默跳过——
                # 否则勾了确认却什么也没发生。
                membership.status = "included"
                membership.trash_reason = None
                added += 1
            else:
                skipped += 1
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
                        TopicPaper.topic_id == topic_id,
                        TopicPaper.paper_id == paper.id,
                        # 回收站里的旧行不算「已入架」：add_to_shelf 会把它复活
                        TopicPaper.trashed_at.is_(None),
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


async def sync_status(session: AsyncSession) -> dict[str, Any]:
    """每日论文池的同步健康状况（给用户看的，不是给运维看的）。

    每日池现在是所有文献库的唯一供给：池子空了，全实验室当天什么都收不到。所以
    「上次同步是什么时候、有没有分类失败、池子是不是过期了」必须摆在界面上，而不是
    只躺在任务日志里等人去翻。

    ``stale`` 的判据是「最新的一天不是今天也不是昨天」：arXiv 周末不公告，只差一天
    属正常，差两天以上才值得提醒。
    """
    run = (
        await session.execute(
            select(VoyageRun)
            .where(VoyageRun.kind == DAILY_FEED_VOYAGE_KIND)
            .order_by(VoyageRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    per_category: dict[str, Any] = {}
    failed: list[str] = []
    if run is not None:
        step = (
            await session.execute(
                select(VoyageStep)
                .where(VoyageStep.run_id == run.id, VoyageStep.action == "daily.fetch")
                .limit(1)
            )
        ).scalar_one_or_none()
        obs = (step.observation if step is not None else None) or {}
        per_category = obs.get("per_category") or {}
        failed = list(obs.get("failed_categories") or [])

    latest = await session.scalar(select(func.max(DailyFeedEntry.feed_date)))
    now = dt.datetime.now(dt.UTC)
    today = now.date()
    # 今天还没抓到新批次时，界面上要能区分「还在探」和「探完了今天就是没有」——
    # 否则用户只看到「最新是昨天」，无从判断该等还是该查。
    probe = await probe_state(session, now=now)
    state = feed_state(
        latest=latest,
        today=today,
        probe=probe,
        max_attempts=await get_max_probe_attempts(session),
        failed=bool(failed),
    )
    return {
        "latest_feed_date": latest.isoformat() if latest else None,
        "feed_state": state,
        # stale 保留旧口径的语义（「该提醒人了」），但不再把周末与等待中的情况算进来
        "stale": state == "stalled",
        "last_run_id": str(run.id) if run is not None else None,
        "last_run_status": run.status if run is not None else None,
        "last_run_at": run.created_at.isoformat() if run is not None else None,
        "per_category": per_category,
        "failed_categories": failed,
        "probe_attempts": probe["attempts"],
        "probe_max_attempts": await get_max_probe_attempts(session),
        "probe_batch_date": probe["batch_date"],
        "probe_exhausted": probe["exhausted"],
    }


#: arXiv 的 RSS skipDays 里写明周六周日不公告；这两天没有新批次是**正常**的。
_PUBLISHING_WEEKDAYS = frozenset({0, 1, 2, 3, 4})


def last_publishing_day(today: dt.date) -> dt.date:
    """今天（含）往回数，第一个 arXiv 会公告的日子。"""
    day = today
    while day.weekday() not in _PUBLISHING_WEEKDAYS:
        day -= dt.timedelta(days=1)
    return day


def feed_state(
    *,
    latest: dt.date | None,
    today: dt.date,
    probe: dict[str, Any],
    max_attempts: int,
    failed: bool = False,
) -> str:
    """池子现在算什么状态：``fresh`` / ``waiting`` / ``quiet`` / ``stalled`` / ``failed``。

    起因是界面上只有一个「已停更」：周日看到「最新 2026-07-31」就报停更，可 arXiv
    周末本来就不公告，什么毛病也没有。把「没更新」拆成三种：

    - ``waiting``：今天该公告，检查点还在探（没探满）。等就是了。
    - ``quiet``：arXiv 自己没有更新的批次——周末/节假日，或者探满了仍是旧批次。
      判据用探到的批次日期与池子最新日期比：两者一致说明我们与 arXiv 同步，
      是**它**没发新的，不是我们没抓到。
    - ``stalled``：该公告的日子过去了，池子却没跟上，也没有「arXiv 没发」的证据。
      这才是要提醒人去查的那种。
    """
    if failed:
        return "failed"
    if latest is None:
        return "stalled"
    expected = last_publishing_day(today)
    if latest >= expected:
        return "fresh"
    # 今天不该公告（周末），而池子已经跟上了最后一个公告日 → 正常的安静
    if today.weekday() not in _PUBLISHING_WEEKDAYS:
        return "quiet"
    probe_batch = probe.get("batch_date")
    if probe_batch and probe_batch == latest.isoformat():
        # 探到的最新批次就是池子里这一批：arXiv 没发新的，我们没落后
        return "quiet" if probe.get("exhausted") else "waiting"
    if int(probe.get("attempts") or 0) < max_attempts:
        return "waiting"
    return "stalled"


# ---- 抓取时刻（可配置） ----

SYNC_TIME_SETTING_KEY = "daily_feed_sync_time"

#: 默认**开始探测**的时刻（UTC）= 北京时间 09:30。
#:
#: 这不是"几点抓"，是"几点开始每 15 分钟探一次"。arXiv 的实际发布时刻会飘：RSS 自己
#: 写着 ``pubDate: 00:00 -0400`` = 04:00 UTC（北京 12:00），实测 ``lastBuildDate``
#: 也在 04:00 UTC 附近，但没有任何保证。定死一个时刻就是在赌它不飘——赌输的表现是
#: 拿到上一批、去重后一条不进、每一步却都报成功（生产上就是这么连丢两天的）。
#: 改成从早探到晚：探到的批次日期不是今天就什么都不做，等下一个检查点。
DEFAULT_SYNC_UTC = (1, 30)

#: 库同步排在抓取之后多久。每日池是所有文献库的唯一供给，抓取没跑完就同步等于空跑一轮。
LIBRARY_SYNC_DELAY_MINUTES = 90


async def get_sync_time(session: AsyncSession) -> tuple[int, int]:
    """抓取时刻（UTC 时、分）。存量值非法时回落默认。"""
    row = await session.get(SystemSetting, SYNC_TIME_SETTING_KEY)
    value = row.value if row is not None else None
    if isinstance(value, str) and ":" in value:
        hh, _, mm = value.partition(":")
        try:
            hour, minute = int(hh), int(mm)
        except ValueError:
            return DEFAULT_SYNC_UTC
        if 0 <= hour < 24 and 0 <= minute < 60:
            return hour, minute
    return DEFAULT_SYNC_UTC


async def set_sync_time(session: AsyncSession, hour: int, minute: int) -> tuple[int, int]:
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"invalid time: {hour}:{minute}")
    value = f"{hour:02d}:{minute:02d}"
    row = await session.get(SystemSetting, SYNC_TIME_SETTING_KEY)
    if row is None:
        session.add(SystemSetting(key=SYNC_TIME_SETTING_KEY, value=value))
    else:
        row.value = value
    await session.commit()
    return hour, minute


# ---- 探测次数上限（可配置） ----

MAX_PROBE_SETTING_KEY = "daily_feed_max_probe_attempts"
PROBE_STATE_SETTING_KEY = "daily_feed_probe_state"

#: 一天最多探几次。探测每 15 分钟一次，10 次 = 从开始时刻起覆盖 2.5 小时。
#: 有上限是因为「今天 arXiv 就是没发」是正常情况（周末、节假日、发布故障），
#: 不该让检查点从早探到晚——探满这个次数就当天收工，明天重新开始。
DEFAULT_MAX_PROBE_ATTEMPTS = 10


async def get_max_probe_attempts(session: AsyncSession) -> int:
    """一天最多探几次（默认 10）。存量值非法时回落默认。"""
    row = await session.get(SystemSetting, MAX_PROBE_SETTING_KEY)
    value = row.value if row is not None else None
    if isinstance(value, int) and 1 <= value <= 96:
        return value
    return DEFAULT_MAX_PROBE_ATTEMPTS


async def set_max_probe_attempts(session: AsyncSession, attempts: int) -> int:
    if not 1 <= attempts <= 96:
        raise ValueError(f"invalid attempts: {attempts}")
    row = await session.get(SystemSetting, MAX_PROBE_SETTING_KEY)
    if row is None:
        session.add(SystemSetting(key=MAX_PROBE_SETTING_KEY, value=attempts))
    else:
        row.value = attempts
    await session.commit()
    return attempts


async def probe_state(session: AsyncSession, *, now: dt.datetime) -> dict[str, Any]:
    """今天探过几次、探到的最新批次是哪天。跨天自动归零。"""
    today = now.astimezone(dt.UTC).date().isoformat()
    row = await session.get(SystemSetting, PROBE_STATE_SETTING_KEY)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    if value.get("date") != today:
        return {
            "date": today,
            "attempts": 0,
            "batch_date": None,
            "exhausted": False,
            "last_probe_at": None,
        }
    return {
        "date": today,
        "attempts": int(value.get("attempts") or 0),
        "batch_date": value.get("batch_date"),
        "exhausted": bool(value.get("exhausted")),
        "last_probe_at": value.get("last_probe_at"),
    }


#: 探满之后的复查间隔（分钟）。arXiv 的 /new 只带**当天那一批**：今天的公告错过了，
#: 明天抓的是明天那批，这一天就永久没有了。所以「探满」只该意味着别再每 15 分钟敲，
#: 不该意味着今天就此收工——一小时一次的复查，代价是一天最多二十来个 RSS 请求，
#: 换的是「arXiv 发晚了」不至于丢掉一整天。
SLOW_PROBE_MINUTES = 60


def should_probe_now(state: dict[str, Any], *, now: dt.datetime, max_attempts: int) -> bool:
    """现在该探一次吗。

    没探满：该探（由调用方的 15 分钟检查点决定频率）。
    探满了：离上次探测够久才探（降频复查，见 :data:`SLOW_PROBE_MINUTES`）。
    """
    if int(state.get("attempts") or 0) < max_attempts:
        return True
    last = state.get("last_probe_at")
    if not last:
        return True
    try:
        last_at = dt.datetime.fromisoformat(str(last))
    except ValueError:
        return True
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=dt.UTC)
    return now - last_at >= dt.timedelta(minutes=SLOW_PROBE_MINUTES)


async def record_probe(
    session: AsyncSession,
    *,
    now: dt.datetime,
    batch_date: str | None,
    exhausted: bool = False,
) -> dict[str, Any]:
    """记一次「探了但今天那批还没出来」。返回记完之后的状态。

    """
    state = await probe_state(session, now=now)
    state["attempts"] += 1
    state["batch_date"] = batch_date
    state["exhausted"] = exhausted
    state["last_probe_at"] = now.astimezone(dt.UTC).isoformat()
    row = await session.get(SystemSetting, PROBE_STATE_SETTING_KEY)
    if row is None:
        session.add(SystemSetting(key=PROBE_STATE_SETTING_KEY, value=state))
    else:
        row.value = dict(state)
    await session.commit()
    return state


async def due_now(session: AsyncSession, *, now: dt.datetime, delay_minutes: int = 0) -> bool:
    """现在是否到了该跑的时刻（供每 15 分钟一次的检查点判断）。

    arq 的 cron 时刻在 worker 启动时就固定了，改设置得重启才生效。所以改成让 cron 高频
    空转、由这里判断是否真的该跑：``now`` 已过今天的目标时刻即为 True。调用方还要自己
    确认今天没跑过（见 :func:`already_ran_today`），否则每个检查点都会重复触发。
    """
    hour, minute = await get_sync_time(session)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    target += dt.timedelta(minutes=delay_minutes)
    return now >= target


async def already_ran_today(session: AsyncSession, kind: str, *, now: dt.datetime) -> bool:
    """今天（UTC）是否已经建过这种任务。"""
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    found = await session.scalar(
        select(VoyageRun.id)
        .where(VoyageRun.kind == kind, VoyageRun.created_at >= start)
        .limit(1)
    )
    return found is not None


async def claim_today(session: AsyncSession, key: str, *, now: dt.datetime) -> bool:
    """把「今天跑过了」这件事记下来；今天已被记过则返回 False。

    给不建任务记录、因而无从按 VoyageRun 判重的检查点任务用（如发表匹配）。
    先占位再干活：检查点每 15 分钟一次，不占位就会重复触发。
    """
    today = now.date().isoformat()
    row = await session.get(SystemSetting, key)
    if row is not None and row.value == today:
        return False
    if row is None:
        session.add(SystemSetting(key=key, value=today))
    else:
        row.value = today
    await session.commit()
    return True


async def todays_batch_available(session: AsyncSession) -> tuple[bool, str | None]:
    """arXiv 上有我们还没收的论文吗。返回 (有没有, 这批声明的日期)。

    **判据是内容，不是日期。** 曾经用 channel 的 ``pubDate`` 判「是不是今天那批」，
    结果 arXiv 的 RSS 日期字段会整体滞后一天：网页版明明已经是 8-06 的清单、条目
    id 也是当天的（2608.026xx），而 channel 与 item 的 pubDate 都还写着 8-05。于是
    判据永远为假，池子一直停在昨天，界面上就一直「等待今天的批次」。

    连带的代价还不止于此：日期判据也把 RSS 缓存的写入条件卡死了（永远不「新鲜」），
    于是每次探测都真去打 arXiv——把我们自己打到 429。

    「有没有新论文可收」本来就是这个探测唯一关心的事，直接问它。
    """
    categories = await get_categories(session)
    if not categories:
        return True, None
    try:
        entries, batch_at = await get_arxiv_client().fetch_new(categories[0])
    except Exception:  # noqa: BLE001 — 探测失败不下结论，交给正式抓取去报错
        logger.warning("daily feed probe failed", exc_info=True)
        return True, None

    declared = batch_at.astimezone(dt.UTC).date().isoformat() if batch_at else None
    arxiv_ids = [str(e.get("arxiv_id")) for e in entries if e.get("arxiv_id")]
    if not arxiv_ids:
        # 一条都没有：周末/节假日的正常形态，没什么可收的
        return False, declared

    known = set(
        (
            await session.execute(
                select(Paper.arxiv_id)
                .join(DailyFeedEntry, DailyFeedEntry.paper_id == Paper.id)
                .where(Paper.arxiv_id.in_(arxiv_ids))
            )
        )
        .scalars()
        .all()
    )
    fresh = any(aid not in known for aid in arxiv_ids)
    return fresh, declared


# ---- 保留期（可配置） ----

RETENTION_SETTING_KEY = "daily_feed_retention_days"

#: 每日池默认保留天数。这张表同时是**库同步的取数窗口**（同步全量重扫它），所以保留期
#: 也就是「一个库最多能漏几天还能自愈」——漏掉的那几天只要论文还在窗口内，下次同步
#: 会自动补上；掉出窗口就永久错过，arXiv 那边不会再给第二次。
DEFAULT_RETENTION_DAYS = 14


async def get_retention_days(session: AsyncSession) -> int:
    """保留天数（默认 14）。存量值非法时回落默认。"""
    row = await session.get(SystemSetting, RETENTION_SETTING_KEY)
    value = row.value if row is not None else None
    if isinstance(value, int) and 1 <= value <= 90:
        return value
    return DEFAULT_RETENTION_DAYS


async def set_retention_days(session: AsyncSession, days: int) -> int:
    if not (1 <= days <= 90):
        raise ValueError(f"retention out of range: {days}")
    row = await session.get(SystemSetting, RETENTION_SETTING_KEY)
    if row is None:
        session.add(SystemSetting(key=RETENTION_SETTING_KEY, value=days))
    else:
        row.value = days
    await session.commit()
    return days


# ---- 库同步的扫描范围（可配置） ----

SYNC_SCOPE_SETTING_KEY = "library_sync_scope"

#: 库同步每次扫描每日池的范围：
#:
#: - ``since_last``（默认）：从这个库**上次成功同步**那天算起。正常情况下就是当天那批
#:   （和 ``daily`` 一样快），漏了几天会自动多扫几天（和 ``full`` 一样能自愈）。
#: - ``daily``：只扫当天。最省，但某次同步失败落下的论文**永久错过**——arXiv 不会
#:   再给第二次。
#: - ``full``：扫整张表。最稳，代价是每次重复排序几千篇早就处理过的。
DEFAULT_SYNC_SCOPE = "since_last"
SYNC_SCOPES = ("since_last", "daily", "full")


async def get_sync_scope(session: AsyncSession) -> str:
    row = await session.get(SystemSetting, SYNC_SCOPE_SETTING_KEY)
    value = row.value if row is not None else None
    return value if value in SYNC_SCOPES else DEFAULT_SYNC_SCOPE


async def set_sync_scope(session: AsyncSession, scope: str) -> str:
    if scope not in SYNC_SCOPES:
        raise ValueError(f"unknown scope: {scope}")
    row = await session.get(SystemSetting, SYNC_SCOPE_SETTING_KEY)
    if row is None:
        session.add(SystemSetting(key=SYNC_SCOPE_SETTING_KEY, value=scope))
    else:
        row.value = scope
    await session.commit()
    return scope
