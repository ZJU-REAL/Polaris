"""论文库路由（docs/api-m2.md §1）。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.schemas.paper import PaperDetail, PaperListPage, PaperRead, PaperUpdate
from app.services import papers as papers_service
from app.services import projects as projects_service

router = APIRouter(tags=["papers"])


@router.get("/projects/{project_id}/papers", response_model=PaperListPage)
async def list_papers(
    project_id: uuid.UUID,
    status_filter: str | None = Query(default=None, alias="status"),
    q: str | None = Query(default=None),
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
        sort=sort,
        page=page,
        size=size,
    )
    return PaperListPage(
        items=[PaperRead.model_validate(p) for p in items], total=total, page=page, size=size
    )


@router.get("/papers/{paper_id}", response_model=PaperDetail)
async def get_paper(
    paper_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    paper = await papers_service.get_paper_for_user(
        session, paper_id=paper_id, user_id=user.id, with_concepts=True
    )
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    return PaperDetail.model_validate(paper)


@router.patch("/papers/{paper_id}", response_model=PaperDetail)
async def update_paper(
    paper_id: uuid.UUID,
    data: PaperUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> PaperDetail:
    """人工纳入/排除（status: included | excluded）。"""
    paper = await papers_service.get_paper_for_user(
        session, paper_id=paper_id, user_id=user.id, with_concepts=True
    )
    if paper is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PAPER_NOT_FOUND")
    if data.status is not None:
        paper = await papers_service.set_paper_status(session, paper, data.status)
    return PaperDetail.model_validate(paper)
