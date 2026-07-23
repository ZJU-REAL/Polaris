"""共享方向库路由（P5c 只读 + P6 治理，docs-dev/workspace-ia-redesign.md §2/§5/§6/§7）。

方向库对全实验室可读：读端点只做登录校验、不做课题成员校验。
治理端点（库定义编辑 / 策展人任命）按库级写权限校验：成员 ∪ 策展人 ∪ 平台 admin
（策展人任命仅平台 admin）。批量写/管理入口（ingest、论文管理、概念补建等）仍走
project 作用域端点（鉴权同样接入库级写权限助手）。
个人文献库路由在 ``app/api/library.py``（/me/library），勿混淆。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user, require_admin
from app.core.db import get_session
from app.core.llm.router import get_llm_router
from app.models.library_direction import DirectionLibrary
from app.models.user import User
from app.schemas.libraries import (
    CuratorRead,
    CuratorsUpdate,
    DirectionLibraryDetail,
    DirectionLibrarySummary,
    DirectionLibraryUpdate,
    LibraryBudgetRead,
)
from app.schemas.paper import (
    ConceptRead,
    PaperListPage,
    PaperRead,
    ScoredConcept,
    ScoredPaper,
    SearchResponse,
)
from app.services import concepts as concepts_service
from app.services import ingest as ingest_service
from app.services import libraries as libraries_service
from app.services import papers as papers_service

router = APIRouter(tags=["libraries"])


async def _get_library(session: AsyncSession, library_id: uuid.UUID) -> DirectionLibrary:
    library = await libraries_service.get_library(session, library_id)
    if library is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    return library


async def _get_managed_library(
    session: AsyncSession, library_id: uuid.UUID, user: User
) -> DirectionLibrary:
    """治理端点统一入口：库存在 + 请求者有库级写权限（成员/策展人/admin），否则 403。"""
    library = await _get_library(session, library_id)
    if not await libraries_service.can_manage_library(session, user=user, library=library):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="LIBRARY_MANAGE_FORBIDDEN")
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
    rows = await libraries_service.list_libraries_overview(session, user=user)
    return [DirectionLibrarySummary(**row) for row in rows]


@router.get("/libraries/{library_id}", response_model=DirectionLibraryDetail)
async def get_library(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DirectionLibraryDetail:
    library = await _get_library(session, library_id)
    row = await libraries_service.library_overview(session, library=library, user=user)
    return DirectionLibraryDetail(**row)


@router.patch("/libraries/{library_id}", response_model=DirectionLibraryDetail)
async def update_library(
    library_id: uuid.UUID,
    data: DirectionLibraryUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> DirectionLibraryDetail:
    """编辑库定义（可管理者）：name/statement/cadence/monthly_budget/rubric/anchors。

    过渡期隐式库以库为权威，statement/rubric/anchors/cadence 写时同步回
    project.definition（保持 ingest 兼容），name 同步 project.name。
    """
    library = await _get_managed_library(session, library_id, user)
    fields = data.model_dump(exclude_unset=True)
    if fields:
        library = await libraries_service.update_library(session, library=library, fields=fields)
    row = await libraries_service.library_overview(session, library=library, user=user)
    return DirectionLibraryDetail(**row)


@router.get("/libraries/{library_id}/budget", response_model=LibraryBudgetRead)
async def get_library_budget(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LibraryBudgetRead:
    """本月预算消耗（可管理者）：库侧 LLM 调用（打分/编译/概念定义/向量化）的聚合。"""
    library = await _get_managed_library(session, library_id, user)
    usage = await ingest_service.monthly_library_usage(session, library.id)
    budget = library.monthly_budget
    used = int(usage["total_tokens"])
    return LibraryBudgetRead(
        month=usage["month"],
        monthly_budget=budget,
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        used_tokens=used,
        remaining_tokens=None if not budget else max(0, int(budget) - used),
        exhausted=bool(budget) and used >= int(budget),
    )


@router.get("/libraries/{library_id}/curators", response_model=list[CuratorRead])
async def list_curators(
    library_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[CuratorRead]:
    """策展人名单（界面叫「文献库管理员」）；可管理者可见。"""
    library = await _get_managed_library(session, library_id, user)
    rows = await libraries_service.list_curators(session, library.id)
    return [CuratorRead(**row) for row in rows]


@router.put("/libraries/{library_id}/curators", response_model=list[CuratorRead])
async def set_curators(
    library_id: uuid.UUID,
    data: CuratorsUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> list[CuratorRead]:
    """全量替换策展人名单（仅平台 admin）。"""
    library = await _get_library(session, library_id)
    try:
        rows = await libraries_service.set_curators(
            session, library=library, user_ids=data.user_ids
        )
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(e)) from e
    return [CuratorRead(**row) for row in rows]


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
