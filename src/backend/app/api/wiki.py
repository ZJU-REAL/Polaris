"""Research Wiki 附属路由：检索 / Obsidian 导出 / 引用导出 / 项目统计 / 图谱 / 文献库对话
（docs/api-m2.md §3、§5、§6；docs/api-lit.md §6、§8）。"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user, require_llm_chat
from app.core.db import get_session
from app.core.llm.fake import estimate_tokens
from app.core.llm.router import get_llm_router
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper, PaperChunk
from app.models.project import Project
from app.models.user import User
from app.schemas.graph import GraphResponse
from app.schemas.ingest import ProjectStatsRead
from app.schemas.paper import (
    PaperChatRequest,
    PaperRead,
    ScoredConcept,
    ScoredPaper,
    SearchResponse,
)
from app.services import chunks as chunks_service
from app.services import citations as citations_service
from app.services import concepts as concepts_service
from app.services import graph as graph_service
from app.services import libraries as libraries_service
from app.services import library_chat as library_chat_service
from app.services import papers as papers_service
from app.services import stats as stats_service
from app.services.libraries import get_library_for_project
from app.services.topic_shelf import shelf_paper_ids
from app.services.user_library import personal_paper_ids
from app.services.wiki_export import build_obsidian_zip

logger = logging.getLogger(__name__)

router = APIRouter(tags=["wiki"])

_HEARTBEAT_SECONDS = 15.0


async def _member_project(session: AsyncSession, project_id: uuid.UUID, user: User) -> Project:
    project = await libraries_service.get_managed_project(session, project_id=project_id, user=user)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    return project


@router.get("/projects/{project_id}/search", response_model=SearchResponse)
async def search(
    project_id: uuid.UUID,
    q: str = Query(min_length=1),
    mode: str = Query(default="keyword", pattern="^(keyword|semantic)$"),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SearchResponse:
    await _member_project(session, project_id, user)

    mode_used = "keyword"
    reranked = False
    paper_rows: list = []
    if mode == "semantic" and papers_service.semantic_search_supported(session):
        try:
            vectors = await get_llm_router().embed([q], user_id=user.id, project_id=project_id)
            candidates = await papers_service.semantic_search_papers(
                session,
                project_id=project_id,
                query_vector=vectors[0],
                limit=max(papers_service.RERANK_CANDIDATES, limit),
            )
            mode_used = "semantic"
            # 向量召回 top 30 → rerank 取 top limit；失败降级为纯向量分
            paper_rows, reranked = await papers_service.rerank_paper_rows(
                get_llm_router(),
                query=q,
                rows=candidates,
                limit=limit,
                user_id=user.id,
                project_id=project_id,
            )
        except NotImplementedError:
            mode_used = "keyword"  # embedding 路由的 provider 不支持 → 回退
    if mode_used == "keyword":
        paper_rows = await papers_service.keyword_search_papers(
            session, project_id=project_id, q=q, limit=limit, user_id=user.id
        )
    concept_rows = await papers_service.keyword_search_concepts(
        session, project_id=project_id, q=q, limit=limit
    )

    concepts = []
    for concept, score in concept_rows:
        count = await concepts_service.paper_count_of(session, concept.id)
        concepts.append(
            ScoredConcept(
                id=concept.id,
                project_id=project_id,
                name=concept.name,
                category=concept.category,
                definition=concept.definition,
                paper_count=count,
                score=score,
            )
        )
    extras = await papers_service.paper_extras_map(
        session, paper_ids=[p.id for p, _ in paper_rows], user_id=user.id
    )
    papers = [
        ScoredPaper(**(PaperRead.model_validate(p).model_dump() | extras[p.id]), score=s)
        for p, s in paper_rows
    ]
    return SearchResponse(papers=papers, concepts=concepts, mode_used=mode_used, reranked=reranked)


@router.get("/projects/{project_id}/export/obsidian")
async def export_obsidian(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Response:
    project = await _member_project(session, project_id, user)
    content = await build_obsidian_zip(session, project, user_id=user.id)
    filename = f"{project.slug}-wiki.zip"
    return Response(
        content=content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/projects/{project_id}/export/citations")
async def export_citations(
    project_id: uuid.UUID,
    format_: str = Query(default="bibtex", alias="format", pattern="^(bibtex|csl-json)$"),
    status_filter: str | None = Query(default=None, alias="status"),
    tag: str | None = Query(default=None),
    starred: bool | None = Query(default=None),
    ids: str | None = Query(default=None, description="逗号分隔的论文 id（多选导出）"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> Response:
    """引用导出：BibTeX / CSL-JSON（docs/api-lit.md §6）。

    过滤参数与论文列表一致；缺省导出 status in (compiled, included)；
    ids 指定时按 id 精确导出（多选导出）。
    """
    project = await _member_project(session, project_id, user)
    paper_ids: list[uuid.UUID] | None = None
    if ids:
        try:
            paper_ids = [uuid.UUID(x) for x in ids.split(",") if x.strip()]
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="INVALID_IDS") from e
    papers = await citations_service.papers_for_export(
        session,
        project_id=project_id,
        user_id=user.id,
        status=status_filter,
        tag=tag,
        starred=starred,
        paper_ids=paper_ids,
    )
    if format_ == "bibtex":
        return Response(
            content=citations_service.build_bibtex(papers),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{project.slug}-citations.bib"'},
        )
    return Response(
        content=json.dumps(citations_service.build_csl_json(papers), ensure_ascii=False, indent=2),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{project.slug}-citations.json"'},
    )


def _sse_frame(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _chat_stream_response(
    messages: list,
    sources: list,
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None,
) -> StreamingResponse:
    """文献对话 SSE 骨架：先发 ``sources``，再 stage=reading 流式 ``delta``* → ``done``；
    出错 ``error`` 后关流；空闲发 ``: ping`` 心跳。库/课题/个人三种对话共用。"""
    llm = get_llm_router()

    async def event_stream() -> AsyncIterator[str]:
        yield _sse_frame("sources", {"items": [asdict(s) for s in sources]})
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        async def pump() -> None:
            try:
                async for chunk in llm.stream(
                    "reading", messages, user_id=user_id, project_id=project_id
                ):
                    await queue.put(("delta", chunk))
                await queue.put(("done", None))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 转成 error 事件后关流
                logger.warning("literature chat stream failed", exc_info=True)
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

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/projects/{project_id}/chat")
async def chat_with_library(
    project_id: uuid.UUID,
    data: PaperChatRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_chat),
) -> StreamingResponse:
    """文献库对话（docs/api-lit.md §8）：跨文献检索 + stage=reading 流式回答。

    事件：``sources``（引用来源清单）→ ``delta``* → ``done``；错误 ``error`` 后关流。
    """
    project = await _member_project(session, project_id, user)
    user_id = user.id  # 先快照：检索失败路径的 rollback 会使 ORM 对象过期
    history = [(turn.role, turn.content) for turn in data.history[-20:]]  # 最多 10 轮
    llm = get_llm_router()
    messages, sources = await library_chat_service.build_library_messages(
        session, project=project, question=data.question, history=history, llm=llm, user_id=user_id
    )
    return _chat_stream_response(messages, sources, user_id=user_id, project_id=project_id)


@router.post("/projects/{project_id}/shelf/chat")
async def chat_with_shelf(
    project_id: uuid.UUID,
    data: PaperChatRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_chat),
) -> StreamingResponse:
    """课题相关研究对话：语料 = 该课题相关研究书架上的论文集合。

    检索只覆盖书架论文（不按方向库过滤），没索引的论文降级 TL;DR/摘要。
    事件序列与文献库对话一致（``sources`` → ``delta``* → ``done``）。
    """
    project = await _member_project(session, project_id, user)
    user_id = user.id  # 先快照：检索失败路径的 rollback 会使 ORM 对象过期
    statement = project.statement or project.name
    history = [(turn.role, turn.content) for turn in data.history[-20:]]  # 最多 10 轮
    llm = get_llm_router()
    paper_ids = await shelf_paper_ids(session, project_id=project_id)
    messages, sources = await library_chat_service.build_scoped_messages(
        session,
        statement=statement,
        question=data.question,
        history=history,
        paper_ids=paper_ids,
        llm=llm,
        user_id=user_id,
        project_id=project_id,
    )
    return _chat_stream_response(messages, sources, user_id=user_id, project_id=project_id)


@router.post("/library/chat")
async def chat_with_personal_library(
    data: PaperChatRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_chat),
) -> StreamingResponse:
    """个人文献库对话：语料 = 本人收藏的论文集合（用户级，方向无关）。

    检索只覆盖个人收藏（不按方向库过滤），没索引的论文降级 TL;DR/摘要。
    事件序列与文献库对话一致（``sources`` → ``delta``* → ``done``）。
    """
    user_id = user.id
    history = [(turn.role, turn.content) for turn in data.history[-20:]]  # 最多 10 轮
    llm = get_llm_router()
    paper_ids = await personal_paper_ids(session, user_id=user_id, tab="saved")
    messages, sources = await library_chat_service.build_scoped_messages(
        session,
        statement=None,
        question=data.question,
        history=history,
        paper_ids=paper_ids,
        llm=llm,
        user_id=user_id,
        project_id=None,
    )
    return _chat_stream_response(messages, sources, user_id=user_id, project_id=None)


@router.post("/projects/{project_id}/index/rebuild")
async def rebuild_fulltext_index(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> dict[str, Any]:
    """重建全文分段索引（docs/api-lit.md §8）：给已有全文但缺分段的论文补分段并嵌入。

    幂等：已有分段的论文跳过；新入库论文由 ingest 流水线自动处理，通常无需手动调用。
    """
    await _member_project(session, project_id, user)
    # 分段重建是「某具体库」的维护写操作（计库预算），落在课题起源库上
    library = await get_library_for_project(session, project_id)
    if library is None:
        return {
            "papers_indexed": 0,
            "chunks_created": 0,
            "embedded": 0,
            "embed_error": None,
            "total_chunks": 0,
        }
    chunked_ids = select(PaperChunk.paper_id)
    try:
        papers = (
            (
                await session.execute(
                    select(Paper)
                    .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
                    .where(
                        LibraryPaper.library_id == library.id,
                        Paper.full_text_path.is_not(None),
                        Paper.id.not_in(chunked_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
    except ProgrammingError as e:  # paper_chunks 表还没迁移
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DB_MIGRATION_REQUIRED: 请先执行数据库迁移（make migrate）",
        ) from e
    indexed = 0
    chunk_count = 0
    for paper in papers:
        n = await chunks_service.index_paper_fulltext(session, paper)
        if n:
            indexed += 1
            chunk_count += n
    await session.commit()
    embedded, embed_error = await chunks_service.embed_pending_chunks(
        session,
        library_id=library.id,
        llm=get_llm_router(),
        user_id=user.id,
        project_id=project_id,
    )
    total_chunks = int(
        (
            await session.execute(
                select(func.count())
                .select_from(PaperChunk)
                .join(LibraryPaper, LibraryPaper.paper_id == PaperChunk.paper_id)
                .where(LibraryPaper.library_id == library.id)
            )
        ).scalar_one()
    )
    return {
        "papers_indexed": indexed,
        "chunks_created": chunk_count,
        "embedded": embedded,
        "embed_error": embed_error,
        "total_chunks": total_chunks,
    }


@router.get("/projects/{project_id}/graph", response_model=GraphResponse)
async def project_graph(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> GraphResponse:
    """知识图谱：论文 / 作者 / 概念节点与关联边（确定性构建，不走 LLM）。"""
    await _member_project(session, project_id, user)
    data = await graph_service.project_graph(session, project_id=project_id)
    return GraphResponse(**data)


@router.get("/projects/{project_id}/stats", response_model=ProjectStatsRead)
async def project_stats(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ProjectStatsRead:
    await _member_project(session, project_id, user)
    data = await stats_service.project_stats(session, project_id)
    return ProjectStatsRead(**data)
