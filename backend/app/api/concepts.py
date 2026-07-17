"""概念库路由（docs/api-m2.md §2）。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.llm.router import get_llm_router
from app.models.paper import Concept
from app.models.project import ProjectMember
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

router = APIRouter(tags=["concepts"])


def _concept_read(concept: Concept, paper_count: int) -> ConceptRead:
    return ConceptRead(
        id=concept.id,
        project_id=concept.project_id,
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
    rows = await concepts_service.list_concepts(
        session, project_id=project_id, category=category, q=q
    )
    return [_concept_read(concept, count) for concept, count in rows]


@router.post("/projects/{project_id}/concepts/relink", response_model=ConceptRelinkResult)
async def relink_concepts(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ConceptRelinkResult:
    """全库概念补建：对已编译论文重抽 [[双链]]、建缺失概念并补齐关联（幂等）。

    面向历史数据（编译过但概念上链步骤没跑到的论文）；
    新概念定义批量调一次 LLM，失败降级为占位定义，不阻塞。
    """
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    stats, _papers = await concepts_service.link_all_paper_concepts(
        session, project_id=project_id, llm=get_llm_router(), user_id=user.id
    )
    return ConceptRelinkResult(**stats)


@router.get("/concepts/{concept_id}", response_model=ConceptDetail)
async def get_concept(
    concept_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ConceptDetail:
    stmt = (
        select(Concept)
        .join(ProjectMember, ProjectMember.project_id == Concept.project_id)
        .where(Concept.id == concept_id, ProjectMember.user_id == user.id)
    )
    concept = (await session.execute(stmt)).scalar_one_or_none()
    if concept is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CONCEPT_NOT_FOUND")
    papers = await concepts_service.papers_of_concept(session, concept.id)
    related = await concepts_service.related_concepts(session, concept)
    base = _concept_read(concept, len(papers))
    return ConceptDetail(
        **base.model_dump(),
        wiki_content=concept.wiki_content,
        papers=[ConceptPaperRead.model_validate(p) for p in papers],
        related=[ConceptRelatedRead.model_validate(c) for c, _ in related],
    )
