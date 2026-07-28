"""每日新论文池路由：/daily。

全实验室共享：登录即可浏览/点赞/收录（收录目标各自校验写权限）；
订阅分类管理与手动刷新仅 admin。业务逻辑在 services/daily_feed.py。
"""

import asyncio
import datetime as dt
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
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
    DailyRetentionRead,
    DailyRetentionUpdate,
    DailySyncStatus,
    DailySyncTimeRead,
    DailySyncTimeUpdate,
    LibrarySyncScopeRead,
    LibrarySyncScopeUpdate,
)
from app.schemas.paper import PaperChatRequest
from app.services import citations as citations_service
from app.services import daily_feed as daily_service
from app.services import library_chat as library_chat_service
from app.services import paper_enrich as paper_enrich_service
from app.services import papers as papers_service
from app.services.embedding import embed_query

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/daily", tags=["daily"])

_NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, detail="DAILY_ENTRY_NOT_FOUND")


@router.get("/days", response_model=list[DailyDay])
async def list_days(
    announce: str | None = Query(default=None, pattern="^(new|cross)$"),
    category: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[DailyDay]:
    return [
        DailyDay(**d)
        for d in await daily_service.list_days(
            session, announce=announce, category=category
        )
    ]


@router.get("/papers", response_model=DailyPage)
async def list_papers(
    date: dt.date | None = Query(default=None),
    sort: str = Query(default="likes", pattern="^(likes|date)$"),
    mode: str = Query(default="keyword", pattern="^(keyword|semantic)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    q: str | None = Query(default=None),
    announce: str | None = Query(default=None, pattern="^(new|cross)$"),
    category: str | None = Query(default=None, max_length=32),
    author: str | None = Query(default=None, max_length=200),
    affiliation: str | None = Query(default=None, max_length=200),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailyPage:
    """池内列表 / 检索。

    mode=semantic 且有 q 时走论文向量检索（结果按相关度排序、不分页）；数据库不是
    postgres 或 provider 不支持嵌入时回退关键词，响应里 mode_used 如实反映。
    池论文默认不建向量（管理员开关），语义结果可能不全，覆盖度见 vector_ready/total。
    author/affiliation 是详情面板里点作者、点机构带上来的过滤（JSON 文本包含匹配），
    两种检索方式都生效。
    """
    mode_used = "keyword"
    items: list[dict] = []
    total = 0
    if mode == "semantic" and q and q.strip() and papers_service.semantic_search_supported(session):
        try:
            vector, space = await embed_query(session, q.strip(), user_id=user.id)
            rows = await daily_service.semantic_search_daily(
                session,
                query_vector=vector,
                space=space,
                limit=max(papers_service.RERANK_CANDIDATES, size),
                date=date,
                category=category,
                announce=announce,
                author=author,
                affiliation=affiliation,
            )
            mode_used = "semantic"
            entry_by_paper = {paper.id: entry for entry, paper, _ in rows}
            # 向量召回 → rerank 取前 size（rerank 未配置时降级为纯向量分，见 papers 服务）
            ranked, _reranked = await papers_service.rerank_paper_rows(
                get_llm_router(),
                query=q.strip(),
                rows=[(paper, score) for _, paper, score in rows],
                limit=size,
                user_id=user.id,
            )
            items = await daily_service.entry_items(
                session, [(entry_by_paper[p.id], p) for p, _ in ranked], user_id=user.id
            )
            total = len(items)
        except NotImplementedError:
            mode_used = "keyword"  # embedding 路由的 provider 不支持 → 回退
    if mode_used == "keyword":
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
            author=author,
            affiliation=affiliation,
        )
        return DailyPage(items=items, total=total, page=page, size=size, mode_used=mode_used)
    ready, ready_total = await daily_service.embedding_coverage(session)
    return DailyPage(
        items=items,
        total=total,
        page=1,  # 语义结果按相关度返回一页
        size=size,
        mode_used=mode_used,
        vector_ready=ready,
        vector_total=ready_total,
    )


@router.get("/export/citations")
async def export_daily_citations(
    format_: str = Query(default="bibtex", alias="format", pattern="^(bibtex|csl-json)$"),
    ids: str | None = Query(default=None, description="逗号分隔的论文 id（多选导出）"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Response:
    """导出每日新论文的引用：BibTeX / CSL-JSON。

    范围 = 当前滚动窗口内的每日论文；ids 指定时按 id 精确导出（窗口外的 id 落选）。
    每日推送全实验室共享，登录即可导出。
    """
    paper_ids: list[uuid.UUID] | None = None
    if ids:
        try:
            paper_ids = [uuid.UUID(x) for x in ids.split(",") if x.strip()]
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="INVALID_IDS") from e
    papers = await citations_service.papers_for_daily_export(session, paper_ids=paper_ids)
    if format_ == "bibtex":
        return Response(
            content=citations_service.build_bibtex(papers),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="polaris-daily-citations.bib"'},
        )
    return Response(
        content=json.dumps(citations_service.build_csl_json(papers), ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="polaris-daily-citations.json"'},
    )


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
        if paper is None or await paper_enrich_service.paper_processing_complete(session, paper):
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


@router.get("/sync-status", response_model=DailySyncStatus)
async def get_sync_status(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailySyncStatus:
    """每日论文池的同步状况（全员可读）。

    池子是所有文献库的唯一供给，抓取失败会让全实验室当天颗粒无收——所以这个状态要
    摆在每日页上，而不是只留在任务日志里。
    """
    return DailySyncStatus(**await daily_service.sync_status(session))


@router.get("/sync-scope", response_model=LibrarySyncScopeRead)
async def get_sync_scope(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibrarySyncScopeRead:
    """库同步每次扫描每日池的范围（全员可读）。"""
    return LibrarySyncScopeRead(scope=await daily_service.get_sync_scope(session))


@router.put("/sync-scope", response_model=LibrarySyncScopeRead)
async def set_sync_scope(
    payload: LibrarySyncScopeUpdate,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> LibrarySyncScopeRead:
    """改扫描范围（admin）。

    - ``since_last``（默认）：从这个库**上次成功同步**那天算起。正常情况下就是当天
      那批（和 daily 一样省），漏了几天会自动多扫几天（和 full 一样能自愈）。
    - ``daily``：只扫当天。最省，但某次同步失败落下的论文**永久错过**——arXiv 不会
      再给第二次。
    - ``full``：扫整张每日池表。最稳，代价是每次重复排序几千篇早就处理过的。
    """
    return LibrarySyncScopeRead(scope=await daily_service.set_sync_scope(session, payload.scope))


@router.get("/retention", response_model=DailyRetentionRead)
async def get_retention(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailyRetentionRead:
    """每日池保留天数（全员可读）。"""
    return DailyRetentionRead(days=await daily_service.get_retention_days(session))


@router.put("/retention", response_model=DailyRetentionRead)
async def set_retention(
    payload: DailyRetentionUpdate,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> DailyRetentionRead:
    """改保留天数（admin）。

    这不只是「每日页显示几天」：每日池同时是**库同步的取数窗口**，同步会全量重扫它。
    所以保留期也就是「一个文献库最多能漏几天还能自愈」——掉出窗口的论文永久错过，
    arXiv 那边不会再给第二次。调小要谨慎。
    """
    return DailyRetentionRead(days=await daily_service.set_retention_days(session, payload.days))


@router.get("/sync-time", response_model=DailySyncTimeRead)
async def get_sync_time(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailySyncTimeRead:
    """每日论文抓取时刻（UTC，全员可读）。"""
    hour, minute = await daily_service.get_sync_time(session)
    return DailySyncTimeRead(hour=hour, minute=minute)


@router.put("/sync-time", response_model=DailySyncTimeRead)
async def set_sync_time(
    payload: DailySyncTimeUpdate,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> DailySyncTimeRead:
    """改抓取时刻（admin）。

    arXiv 每天约北京时间 10:00 放新公告，早于这个点跑只会拿到**前一天**的列表。
    库同步与发表匹配的时刻由它派生（+90 / +150 分钟），不必分别设置。
    """
    hour, minute = await daily_service.set_sync_time(session, payload.hour, payload.minute)
    return DailySyncTimeRead(hour=hour, minute=minute)


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


@router.post("/papers/{entry_id}/fetch-pdf", response_model=DailyPaperDetail)
async def fetch_entry_pdf(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DailyPaperDetail:
    """把这篇每日论文的 PDF 下到平台（幂等），下完即可在线阅读。

    每日池论文通常不属于任何库/书架，走不了 /papers/{id}/fetch-pdf 的成员可见性兜底，
    故按 entry 授权单开一条；每日推送全实验室共享，登录即可。
    """
    try:
        await daily_service.fetch_entry_pdf(session, entry_id=entry_id, user_id=user.id)
    except daily_service.DailyEntryNotFoundError as exc:
        raise _NOT_FOUND from exc
    except papers_service.PdfSourceUnsupportedError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="PDF_SOURCE_UNSUPPORTED") from e
    except papers_service.PdfFetchFailedError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="PDF_FETCH_FAILED") from e
    item = await daily_service.get_entry_item(session, entry_id=entry_id, user_id=user.id)
    return DailyPaperDetail(**item)


@router.post("/papers/{entry_id}/compile", response_model=DailyCompileResult)
async def compile_entry(
    entry_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_task),
) -> DailyCompileResult:
    """按需编译单篇解读（全平台一份，写 paper_wikis）；进行中 → 409。

    已有解读也能再编译：覆盖同一行，以最新一次为准。"""
    try:
        wiki = await daily_service.compile_entry_wiki(session, entry_id=entry_id, user_id=user.id)
    except daily_service.DailyEntryNotFoundError as exc:
        raise _NOT_FOUND from exc
    except daily_service.CompileInProgressError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="COMPILE_IN_PROGRESS") from None
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — LLM 空响应/调用失败等 → 502
        logger.warning("daily wiki compile failed for entry %s", entry_id, exc_info=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="COMPILE_FAILED") from e
    return DailyCompileResult(entry_id=entry_id, wiki_content=wiki.content, model=wiki.model)


@router.post("/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
    queue: TaskQueue = Depends(get_task_queue),
) -> dict[str, str]:
    """手动触发一次抓取（验证/补抓用）：建任务 + 入队，返回 voyage_id 供跳转任务详情。

    互斥是全局单例——已有一次抓取在跑就 409（比按天去重的 job_id 更准：同一天里
    上一次失败后仍可立刻重来，而正在跑的绝不会被重复触发）。
    """
    try:
        run = await daily_service.create_daily_feed_voyage(session, created_by=user.id)
    except daily_service.DailyFeedConflictError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="DAILY_FEED_RUNNING") from e
    await queue.enqueue("run_voyage", str(run.id))
    return {"status": "queued", "voyage_id": str(run.id)}
