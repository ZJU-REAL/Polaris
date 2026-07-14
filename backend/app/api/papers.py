"""论文库路由（docs/api-m2.md §1、docs/api-lit.md §1/§3/§4/§5）。"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.llm.fake import estimate_tokens
from app.core.llm.router import get_llm_router
from app.models.paper import Paper
from app.models.user import User
from app.schemas.paper import (
    PaperChatRequest,
    PaperDetail,
    PaperListPage,
    PaperManualCreate,
    PaperMyMetaRead,
    PaperMyMetaUpdate,
    PaperRead,
    PaperTagsUpdate,
    PaperUpdate,
    TagRead,
)
from app.services import paper_import as paper_import_service
from app.services import papers as papers_service
from app.services import projects as projects_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["papers"])

_HEARTBEAT_SECONDS = 15.0


async def _reads_with_extras(
    session: AsyncSession,
    papers: Sequence[Paper],
    user_id: uuid.UUID,
    *,
    detail: bool = False,
) -> list[PaperRead]:
    """ORM → schema，回填 tags/starred/reading_status/note_count（聚合查询，避免 N+1）。"""
    extras = await papers_service.paper_extras_map(
        session, paper_ids=[p.id for p in papers], user_id=user_id
    )
    model = PaperDetail if detail else PaperRead
    return [model.model_validate(p).model_copy(update=extras[p.id]) for p in papers]


async def _paper_detail(session: AsyncSession, paper: Paper, user_id: uuid.UUID) -> PaperDetail:
    (detail,) = await _reads_with_extras(session, [paper], user_id, detail=True)
    return detail  # type: ignore[return-value]


async def _get_member_paper(
    session: AsyncSession, paper_id: uuid.UUID, user: User, *, with_concepts: bool = False
) -> Paper:
    paper = await papers_service.get_paper_for_user(
        session, paper_id=paper_id, user_id=user.id, with_concepts=with_concepts
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
    starred: bool | None = Query(default=None),
    reading_status: str | None = Query(default=None, pattern="^(unread|reading|read)$"),
    sort: str = Query(default="relevance", pattern="^(relevance|-published_at)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperListPage:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    items, total = await papers_service.list_papers(
        session,
        project_id=project_id,
        status=status_filter,
        q=q,
        tag=tag,
        starred=starred,
        reading_status=reading_status,
        user_id=user.id,
        sort=sort,
        page=page,
        size=size,
    )
    return PaperListPage(
        items=await _reads_with_extras(session, items, user.id), total=total, page=page, size=size
    )


@router.post(
    "/projects/{project_id}/papers", response_model=PaperDetail, status_code=status.HTTP_201_CREATED
)
async def add_paper_manually(
    project_id: uuid.UUID,
    data: PaperManualCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Any:
    """手动添加文献：arxiv_id / doi / bibtex 三选一（docs/api-lit.md §4）。"""
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    try:
        paper = await paper_import_service.add_manual_paper(
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
    # 重新加载（带 concepts eager load），避免序列化时触发 async lazy load
    paper = await papers_service.get_paper_for_user(
        session, paper_id=paper.id, user_id=user.id, with_concepts=True
    )
    return await _paper_detail(session, paper, user.id)


@router.get("/papers/{paper_id}", response_model=PaperDetail)
async def get_paper(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    paper = await _get_member_paper(session, paper_id, user, with_concepts=True)
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


# ---- PDF 阅读（docs/api-lit.md §1） ----


@router.get("/papers/{paper_id}/pdf")
async def get_paper_pdf(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> FileResponse:
    paper = await _get_member_paper(session, paper_id, user)
    if not paper.pdf_path or not Path(paper.pdf_path).exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PDF_NOT_AVAILABLE")
    return FileResponse(paper.pdf_path, media_type="application/pdf", filename=f"{paper_id}.pdf")


@router.post("/papers/{paper_id}/fetch-pdf", response_model=PaperDetail)
async def fetch_paper_pdf(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    """按需补下 PDF + 抽全文；已有 PDF 时幂等直接返回。"""
    paper = await _get_member_paper(session, paper_id, user, with_concepts=True)
    try:
        paper = await papers_service.fetch_pdf(session, paper)
    except papers_service.PdfSourceUnsupportedError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="PDF_SOURCE_UNSUPPORTED") from e
    except papers_service.PdfFetchFailedError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="PDF_FETCH_FAILED") from e
    return await _paper_detail(session, paper, user.id)


# ---- AI 伴读（docs/api-lit.md §3，SSE 流） ----


def _sse_frame(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/papers/{paper_id}/chat")
async def chat_with_paper(
    paper_id: uuid.UUID,
    data: PaperChatRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> StreamingResponse:
    """AI 伴读：stage=reading 流式回答；事件 delta/done/error + 15s 心跳注释。"""
    paper = await _get_member_paper(session, paper_id, user)
    history = [(turn.role, turn.content) for turn in data.history[-20:]]  # 最多 10 轮
    messages = papers_service.build_chat_messages(paper, question=data.question, history=history)
    project_id = paper.project_id
    llm = get_llm_router()

    async def event_stream() -> AsyncIterator[str]:
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        async def pump() -> None:
            try:
                # router.stream 结束时自动写 LLMUsage 记账（归属 user + project）
                async for chunk in llm.stream(
                    "reading", messages, user_id=user.id, project_id=project_id
                ):
                    await queue.put(("delta", chunk))
                await queue.put(("done", None))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 转成 error 事件后关流
                logger.warning("paper chat stream failed", exc_info=True)
                await queue.put(("error", f"{type(e).__name__}: {e}"))

        task = asyncio.create_task(pump())
        collected: list[str] = []
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=_HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if kind == "delta":
                    collected.append(payload or "")
                    yield _sse_frame("delta", {"text": payload})
                elif kind == "done":
                    # usage 与 router 记账口径一致（provider 无 usage 时按 len/4 估算）
                    usage = {
                        "prompt_tokens": sum(estimate_tokens(m.content) for m in messages),
                        "completion_tokens": estimate_tokens("".join(collected)),
                    }
                    yield _sse_frame("done", {"usage": usage})
                    return
                else:  # error
                    yield _sse_frame("error", {"detail": payload})
                    return
        finally:
            task.cancel()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 关缓冲
        },
    )


# ---- 标签 / 个人状态（docs/api-lit.md §5） ----


@router.get("/projects/{project_id}/tags", response_model=list[TagRead])
async def list_project_tags(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[TagRead]:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    rows = await papers_service.list_project_tags(session, project_id=project_id)
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


@router.put("/papers/{paper_id}/my-meta", response_model=PaperMyMetaRead)
async def set_paper_my_meta(
    paper_id: uuid.UUID,
    data: PaperMyMetaUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperMyMetaRead:
    """个人星标 / 阅读状态 upsert。"""
    paper = await _get_member_paper(session, paper_id, user)
    meta = await papers_service.upsert_paper_user_meta(
        session,
        paper=paper,
        user_id=user.id,
        starred=data.starred,
        reading_status=data.reading_status,
    )
    return PaperMyMetaRead(starred=meta.starred, reading_status=meta.reading_status)
