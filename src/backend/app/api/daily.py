"""每日新论文池路由：/daily。

全实验室共享：登录即可浏览/点赞/收录（收录目标各自校验写权限）；
订阅分类管理与手动刷新仅 admin。业务逻辑在 services/daily_feed.py。
"""

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user, require_admin
from app.core.db import get_session
from app.core.queue import TaskQueue, get_task_queue
from app.models.user import User
from app.schemas.daily import (
    DailyCategoriesRead,
    DailyCategoriesUpdate,
    DailyCollectionsRead,
    DailyCollectRequest,
    DailyCollectResponse,
    DailyDay,
    DailyLikerFull,
    DailyLikeState,
    DailyPage,
    DailyPaperDetail,
)
from app.services import daily_feed as daily_service

router = APIRouter(prefix="/daily", tags=["daily"])

_NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, detail="DAILY_ENTRY_NOT_FOUND")


@router.get("/days", response_model=list[DailyDay])
async def list_days(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[DailyDay]:
    return [DailyDay(**d) for d in await daily_service.list_days(session)]


@router.get("/papers", response_model=DailyPage)
async def list_papers(
    date: dt.date | None = Query(default=None),
    sort: str = Query(default="likes", pattern="^(likes|date)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailyPage:
    items, total = await daily_service.list_papers(
        session, user_id=user.id, date=date, sort=sort, page=page, size=size, q=q
    )
    return DailyPage(items=items, total=total, page=page, size=size)


@router.get("/liked", response_model=DailyPage)
async def list_my_liked(
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailyPage:
    items, total = await daily_service.list_my_liked(session, user_id=user.id, page=page, size=size)
    return DailyPage(items=items, total=total, page=page, size=size)


@router.get("/papers/{entry_id}", response_model=DailyPaperDetail)
async def get_paper(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailyPaperDetail:
    try:
        item = await daily_service.get_entry_item(session, entry_id=entry_id, user_id=user.id)
    except daily_service.DailyEntryNotFoundError as exc:
        raise _NOT_FOUND from exc
    return DailyPaperDetail(**item)


@router.put("/papers/{entry_id}/like", response_model=DailyLikeState)
async def like_paper(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailyLikeState:
    try:
        state = await daily_service.set_like(
            session, entry_id=entry_id, user_id=user.id, liked=True
        )
    except daily_service.DailyEntryNotFoundError as exc:
        raise _NOT_FOUND from exc
    return DailyLikeState(**state)


@router.delete("/papers/{entry_id}/like", response_model=DailyLikeState)
async def unlike_paper(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailyLikeState:
    try:
        state = await daily_service.set_like(
            session, entry_id=entry_id, user_id=user.id, liked=False
        )
    except daily_service.DailyEntryNotFoundError as exc:
        raise _NOT_FOUND from exc
    return DailyLikeState(**state)


@router.get("/papers/{entry_id}/likers", response_model=list[DailyLikerFull])
async def list_likers(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[DailyLikerFull]:
    try:
        rows = await daily_service.list_likers(session, entry_id=entry_id)
    except daily_service.DailyEntryNotFoundError as exc:
        raise _NOT_FOUND from exc
    return [DailyLikerFull(**r) for r in rows]


@router.get("/papers/{entry_id}/collections", response_model=DailyCollectionsRead)
async def get_collections(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailyCollectionsRead:
    try:
        data = await daily_service.entry_collections(session, entry_id=entry_id, user_id=user.id)
    except daily_service.DailyEntryNotFoundError as exc:
        raise _NOT_FOUND from exc
    return DailyCollectionsRead(**data)


@router.post("/collect", response_model=DailyCollectResponse)
async def collect(
    payload: DailyCollectRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailyCollectResponse:
    results = await daily_service.collect_papers(
        session,
        user=user,
        paper_ids=payload.paper_ids,
        direction_library_ids=payload.direction_library_ids,
        topic_ids=payload.topic_ids,
        personal=payload.personal,
    )
    return DailyCollectResponse(results=results)


@router.get("/categories", response_model=DailyCategoriesRead)
async def get_categories(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailyCategoriesRead:
    return DailyCategoriesRead(categories=await daily_service.get_categories(session))


@router.put("/categories", response_model=DailyCategoriesRead)
async def set_categories(
    payload: DailyCategoriesUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> DailyCategoriesRead:
    try:
        categories = await daily_service.set_categories(session, payload.categories)
    except daily_service.InvalidCategoryError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"INVALID_CATEGORY:{exc.category}"
        ) from exc
    return DailyCategoriesRead(categories=categories)


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
    queue: TaskQueue = Depends(get_task_queue),
) -> dict[str, str]:
    """手动触发一次同步（验证/补抓用）；job_id 按天去重防重复入队。"""
    today = dt.datetime.now(dt.UTC).date().isoformat()
    await queue.enqueue("daily_feed_sync", _job_id=f"daily-feed-manual-{today}")
    return {"status": "queued"}
