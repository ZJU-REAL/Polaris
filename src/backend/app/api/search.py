"""全局搜索路由（顶栏 ⌘K）：GET /global-search?q=。

**不挂课题。** 这里以前是 ``/projects/{project_id}/global-search``，只搜当前课题
关联的库——于是一篇好端端收录在某个独立库里的论文，站在别的课题上就是搜不到，
而界面不会说明原因，看起来就像根本没收录。搜索的作用域应当等于「我够得着什么」，
判据与列表页、详情页共用（见 services/search.global_search 的 docstring）。

按课题检索的那份口径仍然保留，供 agent 工具 ``global_search`` 使用——它检索的
本来就是课题内的想法/实验/稿件。
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.schemas.search import GlobalSearchResponse
from app.services import search as search_service

router = APIRouter(tags=["search"])


@router.get("/global-search", response_model=GlobalSearchResponse)
async def global_search(
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=5, ge=1, le=20, description="每类结果数上限"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> GlobalSearchResponse:
    hits = await search_service.global_search(
        session, user_id=user.id, q=q, limit_per_type=limit
    )
    return GlobalSearchResponse(query=q, hits=hits)
