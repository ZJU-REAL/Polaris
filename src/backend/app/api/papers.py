"""论文库路由（docs/api-m2.md §1、docs/api-lit.md §1/§3/§4/§5）。"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user, require_llm_chat, require_llm_task
from app.core.db import get_session
from app.core.llm.fake import estimate_tokens
from app.core.llm.router import get_llm_router
from app.models.paper import Paper
from app.models.user import User
from app.schemas.paper import (
    PaperBatchIds,
    PaperChatRequest,
    PaperDetail,
    PaperFigure,
    PaperFiguresResponse,
    PaperListPage,
    PaperManualCreate,
    PaperMyMetaRead,
    PaperMyMetaUpdate,
    PaperRead,
    PaperTagsUpdate,
    PaperUpdate,
    PersonalWikiRead,
    PersonalWikiRequest,
    TagRead,
)
from app.services import figure_annotate as figure_service
from app.services import libraries as libraries_service
from app.services import library_chat as library_chat_service
from app.services import paper_import as paper_import_service
from app.services import papers as papers_service
from app.services import personal_wiki as personal_wiki_service
from app.services import projects as projects_service
from app.services import relevance as relevance_service
from app.services import wiki_compile as wiki_compile_service
from app.services.literature.pdf_extract import figure_path

logger = logging.getLogger(__name__)

router = APIRouter(tags=["papers"])

_HEARTBEAT_SECONDS = 15.0


async def _reads_with_extras(
    session: AsyncSession,
    papers: Sequence[papers_service.PaperView],
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


async def _paper_detail(
    session: AsyncSession, paper: papers_service.PaperView, user_id: uuid.UUID
) -> PaperDetail:
    (detail,) = await _reads_with_extras(session, [paper], user_id, detail=True)
    return detail  # type: ignore[return-value]


async def _get_member_project(session: AsyncSession, project_id: uuid.UUID, user: User):
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    return project


async def _get_member_paper(
    session: AsyncSession, paper_id: uuid.UUID, user: User, *, with_concepts: bool = False
) -> papers_service.PaperView:
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
    author: str | None = Query(default=None, description="作者姓名（包含匹配）"),
    affiliation: str | None = Query(default=None, description="发表机构（包含匹配）"),
    published_from: datetime | None = Query(default=None),
    published_to: datetime | None = Query(default=None),
    created_from: datetime | None = Query(default=None),
    created_to: datetime | None = Query(default=None),
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
        author=author,
        affiliation=affiliation,
        published_from=published_from,
        published_to=published_to,
        created_from=created_from,
        created_to=created_to,
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
    # 打分失败会 rollback 并使本 session 的 ORM 对象过期，先留住要用的 id
    paper_id, user_id = paper.id, user.id
    library = await libraries_service.get_library_for_project(session, project_id)
    membership = await libraries_service.get_membership(
        session, library_id=library.id, paper_id=paper_id
    )
    # 顺带相关性打分（best-effort）：失败只记日志，不影响 201；不改 status（人工纳入）
    await relevance_service.score_added_paper_best_effort(
        session, paper, membership, project, user_id=user_id
    )
    # 重新加载（带 concepts eager load），避免序列化时触发 async lazy load
    view = await papers_service.get_paper_for_user(
        session, paper_id=paper_id, user_id=user_id, with_concepts=True
    )
    return await _paper_detail(session, view, user_id)


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


@router.delete("/papers/{paper_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_paper(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """彻底删除论文（垃圾桶里的「彻底删除」）：清理落盘文件，关联数据级联删除。"""
    paper = await _get_member_paper(session, paper_id, user)
    await papers_service.delete_paper(session, paper)


@router.post("/papers/{paper_id}/restore", response_model=PaperDetail)
async def restore_paper(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    """从垃圾桶召回：已编译回 compiled、打过分回 scored、否则按人工精选。"""
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

    默认软删（移入垃圾桶，可召回）；hard=true 彻底删除。
    """
    await _get_member_project(session, project_id, user)
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
    """清空垃圾桶：彻底删除项目内全部已删除论文。"""
    await _get_member_project(session, project_id, user)
    deleted = await papers_service.empty_trash(session, project_id=project_id)
    return {"deleted": deleted}


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
    view = await _get_member_paper(session, paper_id, user, with_concepts=True)
    try:
        await papers_service.fetch_pdf(
            session, view.paper, user_id=user.id, project_id=view.project_id
        )
    except papers_service.PdfSourceUnsupportedError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="PDF_SOURCE_UNSUPPORTED") from e
    except papers_service.PdfFetchFailedError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="PDF_FETCH_FAILED") from e
    return await _paper_detail(session, view, user.id)


# ---- 论文图片（docs/api-lit.md §6.5） ----


@router.get("/papers/{paper_id}/figures", response_model=list[PaperFigure])
async def list_paper_figures(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[PaperFigure]:
    paper = await _get_member_paper(session, paper_id, user)
    return [PaperFigure(**f) for f in (paper.figures or [])]


@router.get("/papers/{paper_id}/figures/{index}/image")
async def get_paper_figure_image(
    paper_id: uuid.UUID,
    index: int,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> FileResponse:
    paper = await _get_member_paper(session, paper_id, user)
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
    user: User = Depends(current_active_user),
) -> PaperFiguresResponse:
    """提取嵌入图 + LLM 筛选注释；已有 figures 且非 force 时幂等直返。"""
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
    """重跑筛选注释 + 图文编译，覆盖 wiki_content；无 PDF 时跳过图片仅重写文字。"""
    paper = await _get_member_paper(session, paper_id, user, with_concepts=True)
    try:
        paper = await wiki_compile_service.recompile_paper(session, paper, user_id=user.id)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — LLM 空响应/调用失败等 → 502
        logger.warning("recompile failed for paper %s", paper_id, exc_info=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="COMPILE_FAILED") from e
    return await _paper_detail(session, paper, user.id)


# ---- 个人版 wiki 按需编译（P5b，docs-dev/workspace-ia-redesign.md §3.3/§4） ----


@router.post("/papers/{paper_id}/personal-wiki", response_model=PersonalWikiRead)
async def compile_personal_wiki(
    paper_id: uuid.UUID,
    data: PersonalWikiRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_task),
) -> PersonalWikiRead:
    """给没有库版 wiki 的池内论文（典型：个人补充入库）编译个人版 wiki。

    通用模板（无 rubric），可选 topic_id 把课题 statement 当侧重提示；
    结果写进本人个人库条目（user_library_entries.wiki_content），费用归个人。
    已有库版 wiki → 409 LIBRARY_WIKI_EXISTS；同一 paper × user 编译进行中 →
    409 COMPILE_IN_PROGRESS。内容池全平台可读，故不要求论文在本人方向库内。
    """
    paper = await session.get(Paper, paper_id)
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    if data.topic_id is not None:
        # 课题归因/侧重提示：必须是自己所在课题
        await _get_member_project(session, data.topic_id, user)
    try:
        compiled = await personal_wiki_service.compile_personal_wiki(
            session, paper=paper, user_id=user.id, topic_id=data.topic_id
        )
    except personal_wiki_service.LibraryWikiExistsError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="LIBRARY_WIKI_EXISTS"
        ) from None
    except personal_wiki_service.CompileInProgressError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="COMPILE_IN_PROGRESS"
        ) from None
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — LLM 空响应/调用失败等 → 502
        logger.warning("personal wiki compile failed for paper %s", paper_id, exc_info=True)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="COMPILE_FAILED") from e
    return PersonalWikiRead(
        paper_id=paper_id, wiki_content=compiled.content, model=compiled.model or None
    )


# ---- AI 伴读（docs/api-lit.md §3，SSE 流） ----


def _sse_frame(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/papers/{paper_id}/chat")
async def chat_with_paper(
    paper_id: uuid.UUID,
    data: PaperChatRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_chat),
) -> StreamingResponse:
    """AI 伴读：stage=reading 流式回答；事件 delta/done/error + 15s 心跳注释。"""
    paper = await _get_member_paper(session, paper_id, user)
    project_id = paper.project_id
    user_id = user.id  # 先快照：检索失败路径的 rollback 会使 ORM 对象过期
    history = [(turn.role, turn.content) for turn in data.history[-20:]]  # 最多 10 轮
    llm = get_llm_router()

    # 用户选中的其他文献：检索相关片段作为对比/参考上下文（跨项目引用被过滤）
    references, sources = "", []
    if data.context_paper_ids:
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

    async def event_stream() -> AsyncIterator[str]:
        if sources:
            yield _sse_frame("sources", {"items": [asdict(s) for s in sources]})
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        async def pump() -> None:
            try:
                # router.stream 结束时自动写 LLMUsage 记账（归属 user + project）
                async for chunk in llm.stream(
                    "reading", messages, user_id=user_id, project_id=project_id
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
