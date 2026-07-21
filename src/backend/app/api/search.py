"""全局搜索路由（顶栏 ⌘K）：GET /projects/{project_id}/global-search?q=。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.schemas.search import GlobalSearchResponse
from app.services import projects as projects_service
from app.services import search as search_service

router = APIRouter(tags=["search"])


@router.get("/projects/{project_id}/global-search", response_model=GlobalSearchResponse)
async def global_search(
    project_id: uuid.UUID,
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=5, ge=1, le=20, description="每类结果数上限"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> GlobalSearchResponse:
    project = await projects_service.get_project(session, project_id=project_id, user_id=user.id)
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    hits = await search_service.global_search(
        session, project_id=project_id, q=q, limit_per_type=limit
    )
    return GlobalSearchResponse(query=q, hits=hits)
