"""每日新论文池路由：/daily。

全实验室共享：登录即可浏览/点赞/收录（收录目标各自校验写权限）；
订阅分类管理与手动刷新仅 admin。业务逻辑在 services/daily_feed.py。
"""

import asyncio
import datetime as dt
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user, require_admin, require_llm_chat, require_llm_task
from app.core.db import get_session
from app.core.llm.router import get_llm_router
from app.core.queue import TaskQueue, get_task_queue
from app.core.redis import get_redis_dep
from app.models.library_direction import DirectionLibrary
from app.models.paper import Paper
from app.models.user import User
from app.schemas.daily import (
    DailyCategoriesRead,
    DailyCategoriesUpdate,
    DailyCollectionsRead,
    DailyCollectRequest,
    DailyCollectResponse,
    DailyCollectTask,
    DailyCompileResult,
    DailyDay,
    DailyLikerFull,
    DailyLikeState,
    DailyPage,
    DailyPaperDetail,
)
from app.schemas.paper import PaperChatRequest
from app.services import daily_feed as daily_service
from app.services import library_chat as library_chat_service
from app.services import paper_enrich as paper_enrich_service

logger = logging.getLogger(__name__)

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
    announce: str | None = Query(default=None, pattern="^(new|cross)$"),
    category: str | None = Query(default=None, max_length=32),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailyPage:
    items, total = await daily_service.list_papers(
        session,
        user_id=user.id,
        date=date,
        sort=sort,
        page=page,
        size=size,
        q=q,
        announce=announce,
        category=category,
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
    redis: Redis = Depends(get_redis_dep),
) -> DailyCollectResponse:
    results = await daily_service.collect_papers(
        session,
        user=user,
        paper_ids=payload.paper_ids,
        direction_library_ids=payload.direction_library_ids,
        topic_ids=payload.topic_ids,
        personal=payload.personal,
    )
    # 与手动添加同款后台补全（#74：下载→抽取→补机构→向量化→打分）——池论文是轻量行，
    # 收录后按第一个成功收录的方向库为打分目标；仅书架/个人收录则无打分目标。
    target_library_id = next(
        (r["target_id"] for r in results if r["target_type"] == "library" and not r["forbidden"]),
        None,
    )
    project_id = None
    if target_library_id is not None:
        library = await session.get(DirectionLibrary, target_library_id)
        project_id = library.project_id if library is not None else None
    tasks: list[DailyCollectTask] = []
    for paper_id in payload.paper_ids:
        paper = await session.get(Paper, paper_id)
        if paper is None or paper_enrich_service.paper_processing_complete(paper):
            continue
        task_id = await paper_enrich_service.launch_paper_enrichment(
            redis=redis,
            paper_id=paper_id,
            user_id=user.id,
            library_id=target_library_id,
            project_id=project_id,
        )
        if task_id:
            tasks.append(DailyCollectTask(paper_id=paper_id, task_id=task_id))
    return DailyCollectResponse(results=results, tasks=tasks)


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


@router.post("/chat")
async def chat_with_daily_pool(
    data: PaperChatRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_chat),
) -> StreamingResponse:
    """池级文献对话：语料 = 当前滚动 7 天池内全部论文（摘要级，不建全文索引）。

    事件序列与文献库对话一致（``sources`` → ``delta``* → ``done``）。
    """
    from app.api.wiki import _chat_stream_response

    history = [(turn.role, turn.content) for turn in data.history[-20:]]  # 最多 10 轮
    paper_ids = await daily_service.daily_paper_ids(session)
    messages, sources = await library_chat_service.build_scoped_messages(
        session,
        statement=None,
        question=data.question,
        history=history,
        paper_ids=paper_ids,
        llm=get_llm_router(),
        user_id=user.id,
        project_id=None,
    )
    return _chat_stream_response(messages, sources, user_id=user.id, project_id=None)


@router.post("/papers/{entry_id}/compile", response_model=DailyCompileResult)
async def compile_entry(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_task),
) -> DailyCompileResult:
    """按需编译单篇解读（通用模板，全实验室共享一份）；进行中 → 409。"""
    try:
        entry = await daily_service.compile_entry_wiki(session, entry_id=entry_id, user_id=user.id)
    except daily_service.DailyEntryNotFoundError as exc:
        raise _NOT_FOUND from exc
    except daily_service.CompileInProgressError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="COMPILE_IN_PROGRESS") from None
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — LLM 空响应/调用失败等 → 502
        logger.warning("daily wiki compile failed for entry %s", entry_id, exc_info=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="COMPILE_FAILED") from e
    return DailyCompileResult(
        entry_id=entry_id, wiki_content=entry.wiki_content or "", model=entry.wiki_model
    )


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
