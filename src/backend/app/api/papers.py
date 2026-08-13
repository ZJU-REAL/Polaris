"""论文库路由（docs/api-m2.md §1、docs/api-lit.md §1/§3/§4/§5）。"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user, require_llm_chat, require_llm_task
from app.api.chat_stream import chat_stream_response
from app.api.chat_stream import sse_frame as _sse_frame
from app.core.db import get_session
from app.core.events import paper_task_channel, paper_task_log_key
from app.core.llm.router import get_llm_router
from app.core.redis import get_redis_dep
from app.models.user import User
from app.schemas.paper import (
    CollectingLibraryRead,
    MyTagRead,
    PaperBatchIds,
    PaperChatRequest,
    PaperDetail,
    PaperFigure,
    PaperFiguresResponse,
    PaperIndexRebuild,
    PaperIndexStatusRead,
    PaperListPage,
    PaperManualBatchCreate,
    PaperManualBatchTaskRead,
    PaperManualCreate,
    PaperMyMetaRead,
    PaperMyMetaUpdate,
    PaperMyTagsRead,
    PaperMyTagsUpdate,
    PaperPdfUrlIn,
    PaperRead,
    PaperTagsUpdate,
    PaperUpdate,
    ResolvedPaperBatchCreate,
    ResolvedPaperBatchItem,
    ResolvedPaperBatchRead,
    ResolvedPaperRead,
    TagRead,
)
from app.services import figure_annotate as figure_service
from app.services import libraries as libraries_service
from app.services import library_chat as library_chat_service
from app.services import paper_enrich as paper_enrich_service
from app.services import paper_import as paper_import_service
from app.services import paper_index as paper_index_service
from app.services import paper_wiki as paper_wiki_service
from app.services import papers as papers_service
from app.services import wiki_compile as wiki_compile_service
from app.services.literature.pdf_extract import figure_path
from app.services.literature.pdf_source import PdfUrlError
from app.services.paper_figure_downloads import InvalidFigureDownloadToken, verify_token

logger = logging.getLogger(__name__)

router = APIRouter(tags=["papers"])

_HEARTBEAT_SECONDS = 15.0
MAX_PDF_UPLOAD_BYTES = 100 * 1024 * 1024


async def _reads_with_extras(
    session: AsyncSession,
    papers: Sequence[papers_service.PaperView],
    user_id: uuid.UUID,
    *,
    detail: bool = False,
    library_ids: Sequence[uuid.UUID] | None = None,
) -> list[PaperRead]:
    """ORM → schema，回填 tags/my_tags/starred/reading_status/note_count（聚合查询，避免 N+1）。

    library_ids = 本次浏览的库上下文（库标签按它过滤，见 paper_extras_map）；不传时按
    每篇论文自己解析到的成员行所属库来算，池级可达的无库论文则不显示库标签。
    """
    extras = await papers_service.paper_extras_map(
        session,
        paper_ids=[p.id for p in papers],
        user_id=user_id,
        library_ids=(
            library_ids
            if library_ids is not None
            else [p.library_id for p in papers if p.library_id is not None]
        ),
    )
    if not detail:
        return [PaperRead.model_validate(p).model_copy(update=extras[p.id]) for p in papers]
    # 详情多带编译者显示名：重新编译会覆盖别人那份，前端据此提示（人被删则留空）
    names = await paper_wiki_service.compiler_names(
        session, (p.paper.wiki.compiled_by for p in papers if p.paper.wiki is not None)
    )
    out: list[PaperRead] = []
    for p in papers:
        by = p.paper.wiki.compiled_by if p.paper.wiki is not None else None
        update = extras[p.id] | {"compiled_by_name": names.get(by) if by else None}
        out.append(PaperDetail.model_validate(p).model_copy(update=update))
    return out


async def _paper_detail(
    session: AsyncSession, paper: papers_service.PaperView, user_id: uuid.UUID
) -> PaperDetail:
    (detail,) = await _reads_with_extras(session, [paper], user_id, detail=True)
    return detail  # type: ignore[return-value]


async def _get_managed_project(session: AsyncSession, project_id: uuid.UUID, user: User):
    """库管理校验（文献管理端点用）：成员 ∪ 策展人 ∪ 平台 admin（P6）。"""
    project = await libraries_service.get_managed_project(session, project_id=project_id, user=user)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    return project


async def _get_member_paper(
    session: AsyncSession,
    paper_id: uuid.UUID,
    user: User,
    *,
    with_concepts: bool = False,
    include_pool: bool = False,
) -> papers_service.PaperView:
    """include_pool 只给读路径开：书架/个人库可达的无库论文可读（P5b），
    写成员行的端点（状态/删除/召回/重编译/标签）维持库可见性。"""
    paper = await papers_service.get_paper_for_user(
        session,
        paper_id=paper_id,
        user_id=user.id,
        with_concepts=with_concepts,
        include_pool=include_pool,
    )
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    return paper


@router.get("/projects/{project_id}/papers", response_model=PaperListPage)
async def list_papers(
    project_id: uuid.UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None),
    tag: str | None = Query(default=None),
    my_tag: str | None = Query(default=None, description="按我的个人标签过滤"),
    starred: bool | None = Query(default=None),
    reading_status: str | None = Query(default=None, pattern="^(unread|reading|read)$"),
    author: str | None = Query(default=None, description="作者姓名（包含匹配）"),
    affiliation: str | None = Query(default=None, description="发表机构（包含匹配）"),
    published_from: datetime | None = Query(default=None),
    published_to: datetime | None = Query(default=None),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
    daily_only: bool = Query(
        default=False, description="只看从每日论文池自动收录的"
    ),
    last_sync_only: bool = Query(
        default=False,
        description="只看最近一次同步新增的（没同步过则返回空；「最新收录」视图用）",
    ),
    sort: str = Query(default="relevance", pattern="^(relevance|-published_at)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperListPage:
    project = await libraries_service.get_managed_project(session, project_id=project_id, user=user)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    items, total = await papers_service.list_papers(
        session,
        project_id=project_id,
        status=status_filter,
        q=q,
        tag=tag,
        my_tag=my_tag,
        starred=starred,
        reading_status=reading_status,
        user_id=user.id,
        sort=sort,
        page=page,
        size=size,
        author=author,
        affiliation=affiliation,
        published_from=published_from,
        published_to=published_to,
        created_from=created_from,
        created_to=created_to,
        daily_only=daily_only,
        last_sync_only=last_sync_only,
    )
    # 库标签按课题的关联库并集显示（与 tag 过滤同口径，翻页也稳定）
    library_ids = await libraries_service.get_source_library_ids(session, project_id)
    return PaperListPage(
        items=await _reads_with_extras(session, items, user.id, library_ids=library_ids),
        total=total,
        page=page,
        size=size,
    )


@router.post(
    "/projects/{project_id}/papers", response_model=PaperDetail, status_code=status.HTTP_201_CREATED
)
async def add_paper_manually(
    project_id: uuid.UUID,
    data: PaperManualCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    redis: Redis = Depends(get_redis_dep),
) -> Any:
    """手动添加文献：arxiv_id / doi / bibtex 三选一（docs/api-lit.md §4）。

    同步只建元数据行；PDF 下载/全文抽取/向量化/相关性打分交给后台任务，响应回传
    task_id 供前端订阅分阶段进度（论文已处理完整时为 null）。
    """
    project = await libraries_service.get_managed_project(session, project_id=project_id, user=user)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    try:
        result = await paper_import_service.add_manual_paper(
            session,
            project_id=project_id,
            arxiv_id=data.arxiv_id,
            doi=data.doi,
            bibtex=data.bibtex,
        )
    except paper_import_service.DuplicatePaperError as e:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "PAPER_EXISTS", "paper_id": str(e.paper_id)},
        )
    except paper_import_service.ParseFailedError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"PARSE_FAILED: {e}"
        ) from e
    paper_id, user_id = result.paper.id, user.id
    # 后台补全：新建或未处理完整时启动（含针对起源库 definition 的打分）
    task_id: str | None = None
    already_done = await paper_enrich_service.paper_processing_complete(session, result.paper)
    if result.created or not already_done:
        library = await libraries_service.get_library_for_project(session, project_id)
        task_id = await paper_enrich_service.launch_paper_enrichment(
            redis=redis,
            paper_id=paper_id,
            user_id=user_id,
            library_id=library.id if library is not None else None,
            project_id=project.id,
        )
    # 重新加载（带 concepts eager load），避免序列化时触发 async lazy load
    view = await papers_service.get_paper_for_user(
        session, paper_id=paper_id, user_id=user_id, with_concepts=True
    )
    detail = await _paper_detail(session, view, user_id)
    return detail.model_copy(update={"task_id": task_id})


@router.post(
    "/projects/{project_id}/paper-imports/batch",
    response_model=PaperManualBatchTaskRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def add_papers_manually_batch(
    project_id: uuid.UUID,
    data: PaperManualBatchCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    redis: Redis = Depends(get_redis_dep),
) -> PaperManualBatchTaskRead:
    """批量添加 1–50 篇文献；逐项提交，部分失败不影响已成功项。"""
    project = await libraries_service.get_managed_project(session, project_id=project_id, user=user)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    library = await libraries_service.get_library_for_project(session, project_id)
    if library is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail="PROJECT_LIBRARY_NOT_FOUND"
        )
    task_id = await paper_enrich_service.launch_paper_batch_import(
        redis=redis,
        items=[item.model_dump() for item in data.items],
        library_id=library.id,
        user_id=user.id,
        project_id=project.id,
    )
    if task_id is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="TASK_SERVICE_UNAVAILABLE")
    return PaperManualBatchTaskRead(task_id=task_id, total=len(data.items))


@router.get("/paper-tasks/{task_id}/events")
async def paper_task_events(
    task_id: str,
    user: User = Depends(current_active_user),
    redis: Redis = Depends(get_redis_dep),
) -> StreamingResponse:
    """SSE：手动添加文献的后台补全进度（下载/抽取/向量化/打分）。

    鉴权：task 归属用户（redis paper_task_owner:{task_id}）须等于当前登录用户，否则 404。
    转发 paper_task 频道事件，15s 心跳，收到 done/error 后结束流。
    """
    owner = await paper_enrich_service.owner_of(redis, task_id)
    if owner is None or owner != str(user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_TASK_NOT_FOUND")

    def _frame_from(raw: Any) -> tuple[str, str] | None:
        """把一条原始事件（bytes/str JSON）解析成 (event, sse_frame)；无效则 None。"""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        event = str(payload.get("event", "message"))
        return event, _sse_frame(event, payload.get("data"))

    async def stream() -> AsyncIterator[str]:
        # 先订阅频道（不漏后续实时事件），再回放持久化历史：pub/sub 不补发，
        # 而任务常在订阅前就发完事件（dev 无网络/LLM 时几毫秒跑完）。回放窗口内
        # 与实时流可能重复个别事件，但前端按 stage 幂等更新、done 有 stopped 卫兵，
        # 重复无害。历史里已含 done/error 说明任务已结束，回放完直接收流。
        pubsub = redis.pubsub()
        await pubsub.subscribe(paper_task_channel(task_id))
        last_ping = time.monotonic()
        try:
            try:
                history = await redis.lrange(paper_task_log_key(task_id), 0, -1)
            except Exception:  # noqa: BLE001 — 回放尽力而为，失败则退化为纯实时
                history = []
            for raw in history:
                parsed = _frame_from(raw)
                if parsed is None:
                    continue
                event, frame = parsed
                yield frame
                if event in ("done", "error"):
                    return
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is not None:
                    parsed = _frame_from(message["data"])
                    if parsed is not None:
                        event, frame = parsed
                        yield frame
                        if event in ("done", "error"):
                            return
                if time.monotonic() - last_ping >= _HEARTBEAT_SECONDS:
                    yield ": ping\n\n"
                    last_ping = time.monotonic()
        except asyncio.CancelledError:
            raise
        finally:
            await pubsub.unsubscribe(paper_task_channel(task_id))
            await pubsub.aclose()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/projects/{project_id}/papers/{paper_id}", response_model=PaperDetail)
async def get_project_paper(
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    """取某篇论文在**课题起源库**那份成员行的详情（课题论文库单篇详情）。

    精确锁定起源库 (library, paper_id) 的成员行——相关度/状态/wiki 都是该库口径，
    不做跨库归并（对照无库作用域的 ``GET /papers/{id}``）。论文不在该课题库 → 404。
    鉴权同课题其它读端点（成员 ∪ 策展人 ∪ admin）。
    """
    await _get_managed_project(session, project_id, user)
    library = await libraries_service.get_library_for_project(session, project_id)
    if library is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    view = await papers_service.get_library_paper_view(
        session,
        library_id=library.id,
        project_id=project_id,
        paper_id=paper_id,
        with_concepts=True,
    )
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    return await _paper_detail(session, view, user.id)


async def _managed_project_library_view(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    user: User,
    with_concepts: bool = False,
) -> papers_service.PaperView:
    """课题写作用域下精确锁定起源库那份成员行（回收站召回/彻底删除用）。

    对照无库作用域的 ``/papers/{id}``：那条走跨库归并，会命中错误的库那份成员行。
    """
    await _get_managed_project(session, project_id, user)
    library = await libraries_service.get_library_for_project(session, project_id)
    if library is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    view = await papers_service.get_library_paper_view(
        session,
        library_id=library.id,
        project_id=project_id,
        paper_id=paper_id,
        with_concepts=with_concepts,
    )
    if view is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    return view


@router.post("/projects/{project_id}/papers/{paper_id}/restore", response_model=PaperDetail)
async def restore_project_paper(
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    """从课题起源库的回收站召回该篇（精确锁定本库成员行，不跨库归并）。"""
    view = await _managed_project_library_view(
        session, project_id=project_id, paper_id=paper_id, user=user, with_concepts=True
    )
    view = await papers_service.restore_paper(session, view)
    return await _paper_detail(session, view, user.id)


@router.delete(
    "/projects/{project_id}/papers/{paper_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_project_paper(
    project_id: uuid.UUID,
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """从课题起源库彻底删除该篇（只删本库成员行，内容池论文/文件/笔记保留）。"""
    view = await _managed_project_library_view(
        session, project_id=project_id, paper_id=paper_id, user=user
    )
    await papers_service.delete_paper(session, view)


@router.get("/papers/resolve", response_model=ResolvedPaperRead)
async def resolve_paper_meta(
    arxiv_id: str = Query(min_length=1),
    _: User = Depends(current_active_user),
) -> ResolvedPaperRead:
    """按 arXiv id 取论文题目等元数据（锚点论文填表用：填 id，题目自动补上）。

    只读 arXiv，不入库、不建向量——纯粹是让人确认「填的这个 id 是不是想要的那篇」。
    解析不出返回 422，前端保留 id、题目留空即可。
    """
    from app.services import paper_import as paper_import_service

    try:
        fields = await paper_import_service.resolve_fields(arxiv_id=arxiv_id)
    except paper_import_service.ParseFailedError as e:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ARXIV_ID_NOT_RESOLVED"
        ) from e
    return ResolvedPaperRead(
        arxiv_id=str(fields.get("arxiv_id") or arxiv_id),
        title=str(fields.get("title") or ""),
        year=fields.get("year"),
        authors=[
            str(a.get("name") if isinstance(a, dict) else a)
            for a in (fields.get("authors") or [])
        ][:8],
    )


@router.post("/papers/resolve-batch", response_model=ResolvedPaperBatchRead)
async def resolve_paper_meta_batch(
    data: ResolvedPaperBatchCreate,
    _: User = Depends(current_active_user),
) -> ResolvedPaperBatchRead:
    """批量解析锚点元数据；单项失败留在结果中，不影响其它 arXiv id。"""
    results = await paper_import_service.resolve_arxiv_fields_batch(data.arxiv_ids)
    return ResolvedPaperBatchRead(
        items=[
            ResolvedPaperBatchItem(
                index=index,
                arxiv_id=str(fields.get("arxiv_id") or data.arxiv_ids[index]),
                title=str(fields.get("title") or ""),
                year=fields.get("year"),
                authors=[
                    str(author.get("name") if isinstance(author, dict) else author)
                    for author in (fields.get("authors") or [])
                ][:8],
                error=fields.get("error"),
            )
            for index, fields in enumerate(results)
        ]
    )


@router.get("/papers/{paper_id}", response_model=PaperDetail)
async def get_paper(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    paper = await _get_member_paper(session, paper_id, user, with_concepts=True, include_pool=True)
    return await _paper_detail(session, paper, user.id)


@router.patch("/papers/{paper_id}", response_model=PaperDetail)
async def update_paper(
    paper_id: uuid.UUID,
    data: PaperUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    """人工纳入/排除（status: included | excluded）。"""
    paper = await _get_member_paper(session, paper_id, user, with_concepts=True)
    if data.status is not None:
        paper = await papers_service.set_paper_status(session, paper, data.status)
    return await _paper_detail(session, paper, user.id)


@router.delete("/papers/{paper_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_paper(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """彻底删除论文（回收站里的「彻底删除」）：清理落盘文件，关联数据级联删除。"""
    paper = await _get_member_paper(session, paper_id, user)
    await papers_service.delete_paper(session, paper)


@router.post("/papers/{paper_id}/restore", response_model=PaperDetail)
async def restore_paper(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    """从回收站召回：已编译回 compiled、打过分回 scored、否则按人工精选。"""
    paper = await _get_member_paper(session, paper_id, user, with_concepts=True)
    paper = await papers_service.restore_paper(session, paper)
    return await _paper_detail(session, paper, user.id)


@router.post("/projects/{project_id}/papers/batch-delete")
async def batch_delete_papers(
    project_id: uuid.UUID,
    data: PaperBatchIds,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict[str, int]:
    """批量删除项目内论文（非本项目的 id 忽略），返回 {deleted}。

    默认软删（移入回收站，可召回）；hard=true 彻底删除。
    """
    await _get_managed_project(session, project_id, user)
    deleted = await papers_service.delete_papers(
        session, project_id=project_id, paper_ids=data.paper_ids, hard=data.hard
    )
    return {"deleted": deleted}


@router.post("/projects/{project_id}/trash/empty")
async def empty_trash(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict[str, int]:
    """清空回收站：彻底删除项目内全部已删除论文。"""
    await _get_managed_project(session, project_id, user)
    deleted = await papers_service.empty_trash(session, project_id=project_id)
    return {"deleted": deleted}


# ---- PDF 阅读（docs/api-lit.md §1） ----


@router.get("/papers/{paper_id}/pdf")
async def get_paper_pdf(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> FileResponse:
    paper = await _get_member_paper(session, paper_id, user, include_pool=True)
    if not paper.pdf_path or not Path(paper.pdf_path).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PDF_NOT_AVAILABLE")
    return FileResponse(
        paper.pdf_path,
        media_type="application/pdf",
        filename=f"{paper_id}.pdf",
        content_disposition_type="inline",
    )


@router.post("/papers/{paper_id}/fetch-pdf", response_model=PaperDetail)
async def fetch_paper_pdf(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    """按需补下 PDF + 抽全文；已有 PDF 时幂等直接返回。"""
    view = await _get_member_paper(session, paper_id, user, with_concepts=True, include_pool=True)
    try:
        await papers_service.fetch_pdf(
            session, view.paper, user_id=user.id, project_id=view.project_id
        )
    except papers_service.PdfSourceUnsupportedError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="PDF_SOURCE_UNSUPPORTED") from e
    except papers_service.PdfFetchFailedError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="PDF_FETCH_FAILED") from e
    return await _paper_detail(session, view, user.id)


@router.post("/papers/{paper_id}/pdf", response_model=PaperDetail)
async def upload_paper_pdf(
    paper_id: uuid.UUID,
    file: UploadFile = File(...),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    """给尚无 PDF 的可见论文上传原件，并同步完成全文抽取、分块与索引。"""
    view = await _get_member_paper(
        session, paper_id, user, with_concepts=True, include_pool=True
    )
    content = await file.read(MAX_PDF_UPLOAD_BYTES + 1)
    if not content:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail="PDF_UPLOAD_EMPTY")
    if len(content) > MAX_PDF_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="PDF_UPLOAD_TOO_LARGE"
        )
    try:
        await papers_service.upload_pdf(
            session,
            view.paper,
            content,
            user_id=user.id,
            project_id=view.project_id,
        )
    except papers_service.PdfAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="PDF_ALREADY_EXISTS") from exc
    except papers_service.PdfUploadInvalidError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="PDF_UPLOAD_INVALID"
        ) from exc
    return await _paper_detail(session, view, user.id)


@router.post("/papers/{paper_id}/pdf-url", response_model=PaperDetail)
async def upload_paper_pdf_from_url(
    paper_id: uuid.UUID,
    data: PaperPdfUrlIn,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    """给尚无 PDF 的可见论文按公开链接取原件；后处理与本地上传完全一致。

    链接不可用（内网、下载失败、拿回来的不是 PDF）统一回 422 并把原因带上——
    这类失败几乎都是用户能自己修的（粘成了落地页而不是 PDF、站点要登录），
    只回一个错误码等于让人猜。
    """
    view = await _get_member_paper(
        session, paper_id, user, with_concepts=True, include_pool=True
    )
    try:
        await papers_service.upload_pdf_from_url(
            session,
            view.paper,
            data.url,
            user_id=user.id,
            project_id=view.project_id,
        )
    except papers_service.PdfAlreadyExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="PDF_ALREADY_EXISTS") from exc
    except PdfUrlError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"PDF_URL_UNUSABLE: {exc}"
        ) from exc
    except papers_service.PdfUploadInvalidError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="PDF_UPLOAD_INVALID"
        ) from exc
    return await _paper_detail(session, view, user.id)


# ---- 论文图片（docs/api-lit.md §6.5） ----


@router.get("/papers/{paper_id}/figures", response_model=list[PaperFigure])
async def list_paper_figures(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[PaperFigure]:
    paper = await _get_member_paper(session, paper_id, user, include_pool=True)
    return [PaperFigure(**f) for f in (paper.figures or [])]


@router.get("/paper-figure-download/{token}")
async def download_paper_figure(
    token: str,
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """Download a figure through a short-lived bearer URL returned by MCP.

    The signature proves who requested the link, while the database lookup makes
    permission revocation take effect even before the URL expires.
    """
    try:
        claims = verify_token(token)
    except InvalidFigureDownloadToken as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="FIGURE_LINK_INVALID") from exc
    user = await session.get(User, claims.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="FIGURE_NOT_FOUND")
    paper = await papers_service.get_paper_for_user(
        session,
        paper_id=claims.paper_id,
        user_id=claims.user_id,
        include_pool=True,
    )
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="FIGURE_NOT_FOUND")
    known = {int(f["index"]) for f in (paper.figures or [])}
    path = figure_path(str(claims.paper_id), claims.index)
    if claims.index not in known or not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="FIGURE_NOT_FOUND")
    return FileResponse(
        path,
        media_type="image/png",
        filename=f"fig_{claims.index}.png",
        content_disposition_type="attachment",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/papers/{paper_id}/figures/{index}/image")
async def get_paper_figure_image(
    paper_id: uuid.UUID,
    index: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> FileResponse:
    paper = await _get_member_paper(session, paper_id, user, include_pool=True)
    known = {int(f["index"]) for f in (paper.figures or [])}
    path = figure_path(str(paper_id), index)
    if index not in known or not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="FIGURE_NOT_FOUND")
    return FileResponse(
        path, media_type="image/png", filename=f"fig_{index}.png", content_disposition_type="inline"
    )


@router.post("/papers/{paper_id}/extract-figures", response_model=PaperFiguresResponse)
async def extract_paper_figures(
    paper_id: uuid.UUID,
    force: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_task),
) -> PaperFiguresResponse:
    """提取嵌入图 + LLM 筛选注释；已有 figures 且非 force 时幂等直返。

    库维护动作（写共享的 paper.figures + 调视觉模型），故与重新编译同口径：
    不开池级兜底，只有对该论文所在库有管理权的人可调。
    """
    view = await _get_member_paper(session, paper_id, user)
    paper = view.paper
    if paper.figures is not None and not force:
        return PaperFiguresResponse(figures=[PaperFigure(**f) for f in paper.figures])
    if not paper.pdf_path or not Path(paper.pdf_path).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PDF_NOT_AVAILABLE")
    figures = await figure_service.extract_and_annotate(
        session, paper, user_id=user.id, project_id=view.project_id
    )
    return PaperFiguresResponse(figures=[PaperFigure(**f) for f in figures])


# ---- 图文交织 wiki 重编译（docs/api-lit.md §6.6，同步调用约 1 分钟） ----


@router.post("/papers/{paper_id}/recompile", response_model=PaperDetail)
async def recompile_paper(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_task),
) -> PaperDetail:
    """重跑筛选注释 + 图文编译，覆盖这篇论文的解读；无 PDF 时跳过图片仅重写文字。

    解读全平台一份：重编译对所有入口生效，谁都能重编，以最新一次为准。"""
    paper = await _get_member_paper(session, paper_id, user, with_concepts=True)
    try:
        paper = await wiki_compile_service.recompile_paper(session, paper, user_id=user.id)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — LLM 空响应/调用失败等 → 502
        logger.warning("recompile failed for paper %s", paper_id, exc_info=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="COMPILE_FAILED") from e
    return await _paper_detail(session, paper, user.id)


# ---- 索引状态与手动重建（docs/api-lit.md §9） ----


@router.get("/papers/{paper_id}/libraries", response_model=list[CollectingLibraryRead])
async def get_collecting_libraries(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[CollectingLibraryRead]:
    """这篇论文被哪些文献库收录了，带各自的相关度分。

    只列对请求者可见的库（个人库不外泄），且只算真正进了库的状态。
    """
    await _get_member_paper(session, paper_id, user, include_pool=True)
    rows = await papers_service.collecting_libraries(session, paper_id, user)
    return [CollectingLibraryRead(**row) for row in rows]


@router.get("/papers/{paper_id}/index-status", response_model=PaperIndexStatusRead)
async def get_paper_index_status(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperIndexStatusRead:
    """这篇论文的论文级向量与分块向量各自建没建、何时建的、用的哪个模型。

    只读，权限与看论文本身一致（能看到就能看状态）。"""
    view = await _get_member_paper(session, paper_id, user, include_pool=True)
    status_ = await paper_index_service.get_index_status(session, view.paper)
    return PaperIndexStatusRead.model_validate(status_, from_attributes=True)


@router.post("/papers/{paper_id}/index/rebuild", response_model=PaperIndexStatusRead)
async def rebuild_paper_index(
    paper_id: uuid.UUID,
    data: PaperIndexRebuild | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_task),
) -> PaperIndexStatusRead:
    """手动重建这篇论文的向量（有就覆盖，没有就新建），同步返回重建后的状态。

    权限取「能看到这篇论文 + 有大模型使用权限」（require_llm_task），与重编译
    （POST /papers/{id}/recompile）同一口径：都花 token、都是谁都能重跑以最新为准，
    没有理由在这里额外要管理权——单篇重建的花费是一次嵌入调用，比重编译低一个量级，
    而配额与锁定用户由 require_llm_task 统一挡住。
    """
    body = data or PaperIndexRebuild()
    targets = {t for t, on in (("paper", body.paper_vector), ("chunks", body.chunks)) if on}
    if not targets:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="NOTHING_TO_REBUILD")
    view = await _get_member_paper(session, paper_id, user, include_pool=True)
    try:
        status_ = await paper_index_service.rebuild_index(
            session,
            view.paper,
            llm=get_llm_router(),
            targets=targets,
            user_id=user.id,
        )
    except paper_index_service.IndexRebuildFailedError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=f"INDEX_REBUILD_FAILED:{e}") from e
    return PaperIndexStatusRead.model_validate(status_, from_attributes=True)


# ---- AI 伴读（docs/api-lit.md §3，SSE 流） ----


@router.post("/papers/{paper_id}/chat")
async def chat_with_paper(
    paper_id: uuid.UUID,
    data: PaperChatRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_chat),
) -> StreamingResponse:
    """AI 伴读：stage=reading 流式回答；事件 delta/done/error + 15s 心跳注释。"""
    paper = await _get_member_paper(session, paper_id, user, include_pool=True)
    project_id = paper.project_id
    user_id = user.id  # 先快照：检索失败路径的 rollback 会使 ORM 对象过期
    history = [(turn.role, turn.content) for turn in data.history[-20:]]  # 最多 10 轮
    llm = get_llm_router()

    # 用户选中的其他文献：检索相关片段作为对比/参考上下文（跨项目引用被过滤）；
    # 池级可达的无库论文没有课题上下文（project_id 为空）→ 跳过参考文献检索
    references, sources = "", []
    if data.context_paper_ids and project_id is not None:
        references, sources = await library_chat_service.build_reference_context(
            session,
            project_id=project_id,
            question=data.question,
            paper_ids=data.context_paper_ids,
            llm=llm,
            user_id=user_id,
        )
    messages = papers_service.build_chat_messages(
        paper, question=data.question, history=history, references=references
    )

    # 伴读只在真有参考文献时才发 sources：它的语料就是当前这一篇
    return chat_stream_response(
        messages,
        sources,
        user_id=user_id,
        project_id=project_id,
        log_label="paper chat",
        always_send_sources=False,
    )


# ---- 标签 / 个人状态（docs/api-lit.md §5） ----


@router.get("/projects/{project_id}/tags", response_model=list[TagRead])
async def list_project_tags(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[TagRead]:
    project = await libraries_service.get_managed_project(session, project_id=project_id, user=user)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    # 课题标签解析到其隐式起源库（无库=无语料，空列表）。
    library = await libraries_service.get_library_for_project(session, project_id)
    if library is None:
        return []
    rows = await papers_service.list_library_tags(session, library_id=library.id)
    return [TagRead(**row) for row in rows]


@router.put("/papers/{paper_id}/tags", response_model=PaperDetail)
async def set_paper_tags(
    paper_id: uuid.UUID,
    data: PaperTagsUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    """整组覆盖论文标签（新名字自动建 tag，空数组=清空）。"""
    paper = await _get_member_paper(session, paper_id, user, with_concepts=True)
    await papers_service.set_paper_tags(session, paper, data.names)
    return await _paper_detail(session, paper, user.id)


@router.get("/me/paper-tags", response_model=list[MyTagRead])
async def list_my_paper_tags(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[MyTagRead]:
    """我的所有个人标签（含标了几篇）——筛选下拉 / 输入建议用。"""
    rows = await papers_service.list_user_paper_tags(session, user_id=user.id)
    return [MyTagRead(**row) for row in rows]


@router.put("/papers/{paper_id}/my-tags", response_model=PaperMyTagsRead)
async def set_paper_my_tags(
    paper_id: uuid.UUID,
    data: PaperMyTagsUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperMyTagsRead:
    """整组覆盖我给这篇打的个人标签（空数组=清空），只有我自己看得到。

    可达口径同 my-meta（include_pool=True）：公共库 / 书架 / 个人库 / 每日推送里
    读得到的论文都能打——个人标签不动共享资产，不需要库管理权限。
    """
    paper = await _get_member_paper(session, paper_id, user, include_pool=True)
    names = await papers_service.set_user_paper_tags(
        session, paper_id=paper.id, user_id=user.id, names=data.names
    )
    return PaperMyTagsRead(my_tags=names)


@router.put("/papers/{paper_id}/my-meta", response_model=PaperMyMetaRead)
async def set_paper_my_meta(
    paper_id: uuid.UUID,
    data: PaperMyMetaUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperMyMetaRead:
    """个人星标 / 阅读状态 upsert。"""
    paper = await _get_member_paper(session, paper_id, user, include_pool=True)
    meta = await papers_service.upsert_paper_user_meta(
        session,
        paper=paper,
        user_id=user.id,
        starred=data.starred,
        reading_status=data.reading_status,
    )
    return PaperMyMetaRead(starred=meta.starred, reading_status=meta.reading_status)
