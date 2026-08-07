"""浏览统计：记一次点击，以及最近 N 天的热度。

三条口径写在这里，因为它们决定了这些数字**能不能拿来当依据**：

1. **同一个人短时间内重复打开只算一次**。不去重的话，热度榜衡量的是「谁刷新得
   最勤」，而不是「哪篇被更多人看」——一个人在一篇论文上来回切标签页就能屠榜。
2. **删掉的对象不出现在榜上，但它的历史记录不删**。事件表不加外键正是为此：
   「它曾被读过」是真实发生过的事；查询时按当前可见对象过滤即可。
3. **只返回总数，不按人展开**。实验室里「谁在读什么」是敏感信息，热度需要的
   只是计数。
"""

import datetime as dt
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import DirectionLibrary
from app.models.paper import Paper
from app.models.user import User
from app.models.view_event import VIEW_KINDS, ViewEvent

#: 去重窗口：同一个人在这段时间内重复打开同一个对象只记一次。
#: 一小时是「读一篇论文」的量级——来回切标签页、刷新、从不同入口再点一次，
#: 都还是同一次阅读。不去重的话，热度榜衡量的是谁刷新得最勤。
DEDUP_MINUTES = 60

#: 热度榜默认看多少天 / 返回多少条。
DEFAULT_DAYS = 7
DEFAULT_LIMIT = 5


async def record_view(
    session: AsyncSession, *, kind: str, target_id: uuid.UUID, user_id: uuid.UUID | None
) -> bool:
    """记一次浏览；被去重窗口挡掉时返回 False。"""
    if kind not in VIEW_KINDS:
        raise ValueError(f"未知的浏览类型：{kind}")
    since = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=DEDUP_MINUTES)
    if user_id is not None:
        recent = await session.scalar(
            select(ViewEvent.id)
            .where(
                ViewEvent.kind == kind,
                ViewEvent.target_id == target_id,
                ViewEvent.user_id == user_id,
                ViewEvent.created_at >= since,
            )
            .limit(1)
        )
        if recent is not None:
            return False
    session.add(ViewEvent(kind=kind, target_id=target_id, user_id=user_id))
    await session.commit()
    return True


async def _counts(
    session: AsyncSession, *, kind: str, days: int, limit: int
) -> list[tuple[uuid.UUID, int]]:
    since = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    rows = await session.execute(
        select(ViewEvent.target_id, func.count(ViewEvent.id).label("views"))
        .where(ViewEvent.kind == kind, ViewEvent.created_at >= since)
        .group_by(ViewEvent.target_id)
        .order_by(func.count(ViewEvent.id).desc())
        # 多取一些：下面还要按可见性过滤，掉队的要有人补位
        .limit(limit * 4)
    )
    return [(row[0], int(row[1])) for row in rows.all()]


async def hot_libraries(
    session: AsyncSession, *, user: User, days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT
) -> list[dict[str, object]]:
    """最近 N 天最常被打开的文献库（只含这个人看得见的）。"""
    from app.services.libraries import library_visible_to

    counts = dict(await _counts(session, kind="library", days=days, limit=limit))
    if not counts:
        return []
    rows = (
        (await session.execute(select(DirectionLibrary).where(DirectionLibrary.id.in_(counts))))
        .scalars()
        .all()
    )
    out = [
        {"id": str(lib.id), "name": lib.name, "views": counts[lib.id]}
        for lib in rows
        if library_visible_to(lib, user)
    ]
    out.sort(key=lambda item: -int(item["views"]))
    return out[:limit]


async def hot_papers(
    session: AsyncSession, *, user: User, days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT
) -> list[dict[str, object]]:
    """最近 N 天最常被打开的论文。"""
    counts = dict(await _counts(session, kind="paper", days=days, limit=limit))
    if not counts:
        return []
    rows = (
        (await session.execute(select(Paper).where(Paper.id.in_(counts)))).scalars().all()
    )
    out = [
        {"id": str(paper.id), "title": paper.title, "views": counts[paper.id]} for paper in rows
    ]
    out.sort(key=lambda item: -int(item["views"]))
    return out[:limit]
