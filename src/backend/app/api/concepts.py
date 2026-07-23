"""概念库路由（docs/api-m2.md §2）。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.llm.router import get_llm_router
from app.models.library_direction import DirectionLibrary
from app.models.paper import Concept
from app.models.user import User
from app.schemas.paper import (
    ConceptDetail,
    ConceptPaperRead,
    ConceptRead,
    ConceptRelatedRead,
    ConceptRelinkResult,
)
from app.services import concepts as concepts_service
from app.services import projects as projects_service
from app.services.libraries import get_library_for_project

router = APIRouter(tags=["concepts"])


def _concept_read(
    concept: Concept, paper_count: int, project_id: uuid.UUID | None
) -> ConceptRead:
    return ConceptRead(
        id=concept.id,
        project_id=project_id,
        name=concept.name,
        category=concept.category,
        definition=concept.definition,
        paper_count=paper_count,
    )


@router.get("/projects/{project_id}/concepts", response_model=list[ConceptRead])
async def list_concepts(
    project_id: uuid.UUID,
    category: str | None = Query(default=None),
    q: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ConceptRead]:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    library = await get_library_for_project(session, project_id)
    rows = await concepts_service.list_concepts(
        session, library_id=library.id, category=category, q=q
    )
    return [_concept_read(concept, count, project_id) for concept, count in rows]


@router.post("/projects/{project_id}/concepts/relink", response_model=ConceptRelinkResult)
async def relink_concepts(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ConceptRelinkResult:
    """全库概念补建：对已编译论文重抽 [[双链]]、建缺失概念并补齐关联（幂等）。

    面向历史数据（编译过但概念上链步骤没跑到的论文）；新概念定义分批调 LLM，
    并回填此前留下的占位概念（「…（定义待补充）」）重新拿定义，失败降级为占位、不阻塞。
    """
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    library = await get_library_for_project(session, project_id)
    stats, _papers = await concepts_service.link_all_paper_concepts(
        session,
        library_id=library.id,
        llm=get_llm_router(),
        user_id=user.id,
        project_id=project_id,
        backfill=True,
    )
    return ConceptRelinkResult(**stats)


@router.get("/concepts/{concept_id}", response_model=ConceptDetail)
async def get_concept(
    concept_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ConceptDetail:
    # P5c：方向库全实验室可读，概念详情不做课题成员校验（登录即可）
    stmt = (
        select(Concept, DirectionLibrary.project_id)
        .join(DirectionLibrary, DirectionLibrary.id == Concept.library_id)
        .where(Concept.id == concept_id)
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CONCEPT_NOT_FOUND")
    concept, project_id = row
    papers = await concepts_service.papers_of_concept(session, concept.id)
    related = await concepts_service.related_concepts(session, concept)
    base = _concept_read(concept, len(papers), project_id)
    return ConceptDetail(
        **base.model_dump(),
        wiki_content=concept.wiki_content,
        papers=[ConceptPaperRead.model_validate(p) for p in papers],
        related=[ConceptRelatedRead.model_validate(c) for c, _ in related],
    )
