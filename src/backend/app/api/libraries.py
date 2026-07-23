"""共享方向库只读路由（P5c，docs-dev/workspace-ia-redesign.md §2/§6/§7）。

方向库对全实验室可读：本文件的端点只做登录校验、不做课题成员校验。
写/管理入口（ingest、论文管理、概念补建等）仍走 project 作用域端点并校验成员。
个人文献库路由在 ``app/api/library.py``（/me/library），勿混淆。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.llm.router import get_llm_router
from app.models.library_direction import DirectionLibrary
from app.models.user import User
from app.schemas.libraries import DirectionLibraryDetail, DirectionLibrarySummary
from app.schemas.paper import (
    ConceptRead,
    PaperListPage,
    PaperRead,
    ScoredConcept,
    ScoredPaper,
    SearchResponse,
)
from app.services import concepts as concepts_service
from app.services import libraries as libraries_service
from app.services import papers as papers_service

router = APIRouter(tags=["libraries"])


async def _get_library(session: AsyncSession, library_id: uuid.UUID) -> DirectionLibrary:
    library = await libraries_service.get_library(session, library_id)
    if library is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    return library


async def _reads_with_extras(
    session: AsyncSession, papers: list, user_id: uuid.UUID
) -> list[PaperRead]:
    """ORM → schema，回填 tags/starred/reading_status/note_count（个人维度，全员可用）。"""
    extras = await papers_service.paper_extras_map(
        session, paper_ids=[p.id for p in papers], user_id=user_id
    )
    return [PaperRead.model_validate(p).model_copy(update=extras[p.id]) for p in papers]


@router.get("/libraries", response_model=list[DirectionLibrarySummary])
async def list_libraries(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[DirectionLibrarySummary]:
    rows = await libraries_service.list_libraries_overview(session, user_id=user.id)
    return [DirectionLibrarySummary(**row) for row in rows]


@router.get("/libraries/{library_id}", response_model=DirectionLibraryDetail)
async def get_library(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DirectionLibraryDetail:
    library = await _get_library(session, library_id)
    row = await libraries_service.library_overview(session, library=library, user_id=user.id)
    return DirectionLibraryDetail(**row)


@router.get("/libraries/{library_id}/papers", response_model=PaperListPage)
async def list_library_papers(
    library_id: uuid.UUID,
    status_filter: str | None = Query(default="library", alias="status"),
    q: str | None = Query(default=None),
    sort: str = Query(default="relevance", pattern="^(relevance|-published_at)$"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperListPage:
    """库内论文（分页/检索/排序）。缺省只列相关性达标的（status=library 组别名）。"""
    library = await _get_library(session, library_id)
    items, total = await papers_service.list_papers(
        session,
        library_id=library.id,
        project_id=library.project_id,
        status=status_filter,
        q=q,
        user_id=user.id,
        sort=sort,
        page=page,
        size=size,
    )
    return PaperListPage(
        items=await _reads_with_extras(session, list(items), user.id),
        total=total,
        page=page,
        size=size,
    )


@router.get("/libraries/{library_id}/concepts", response_model=list[ConceptRead])
async def list_library_concepts(
    library_id: uuid.UUID,
    category: str | None = Query(default=None),
    q: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ConceptRead]:
    library = await _get_library(session, library_id)
    rows = await concepts_service.list_concepts(
        session, library_id=library.id, category=category, q=q
    )
    return [
        ConceptRead(
            id=concept.id,
            project_id=library.project_id,
            name=concept.name,
            category=concept.category,
            definition=concept.definition,
            paper_count=count,
        )
        for concept, count in rows
    ]


@router.get("/libraries/{library_id}/search", response_model=SearchResponse)
async def search_library(
    library_id: uuid.UUID,
    q: str = Query(min_length=1),
    mode: str = Query(default="keyword", pattern="^(keyword|semantic)$"),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SearchResponse:
    """库内检索（关键词/语义）。语义模式的 embed/rerank 记个人账（无课题上下文）。"""
    library = await _get_library(session, library_id)

    mode_used = "keyword"
    reranked = False
    paper_rows: list = []
    if mode == "semantic" and papers_service.semantic_search_supported(session):
        try:
            vectors = await get_llm_router().embed([q], user_id=user.id)
            candidates = await papers_service.semantic_search_papers(
                session,
                library_id=library.id,
                project_id=library.project_id,
                query_vector=vectors[0],
                limit=max(papers_service.RERANK_CANDIDATES, limit),
            )
            mode_used = "semantic"
            paper_rows, reranked = await papers_service.rerank_paper_rows(
                get_llm_router(), query=q, rows=candidates, limit=limit, user_id=user.id
            )
        except NotImplementedError:
            mode_used = "keyword"  # embedding 路由的 provider 不支持 → 回退
    if mode_used == "keyword":
        paper_rows = await papers_service.keyword_search_papers(
            session,
            library_id=library.id,
            project_id=library.project_id,
            q=q,
            limit=limit,
            user_id=user.id,
        )
    concept_rows = await papers_service.keyword_search_concepts(
        session, library_id=library.id, q=q, limit=limit
    )

    concepts = []
    for concept, score in concept_rows:
        count = await concepts_service.paper_count_of(session, concept.id)
        concepts.append(
            ScoredConcept(
                id=concept.id,
                project_id=library.project_id,
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
