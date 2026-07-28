"""实验室数据面板路由（/lab）：索引统计、token 用量与排行榜、跨库图谱。

全部只读、全员可见（登录即可），数字按可见库集合收敛。排行榜额外受管理员开关
``lab_leaderboard_enabled`` 控制：关掉后普通用户拿 403，管理员照常可看。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.schemas.graph import GraphResponse
from app.schemas.lab import (
    DailyIngestStatusRow,
    LabLeaderboardEntry,
    LabLeaderboardResponse,
    LabStats,
)
from app.schemas.llm_admin import UsageRow
from app.services import lab as lab_service
from app.services import llm_admin as llm_admin_service

router = APIRouter(prefix="/lab", tags=["lab"])


@router.get("/stats", response_model=LabStats)
async def lab_stats(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LabStats:
    """实验室索引与内容统计（跨库论文去重；只算当前用户看得到的库）。"""
    return LabStats(**await lab_service.lab_stats(session, user))


@router.get("/daily-ingest", response_model=list[DailyIngestStatusRow])
async def daily_ingest_status(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[DailyIngestStatusRow]:
    """今天各库从每日论文自动收录的进展与结果（只列请求者看得见的库）。

    抓取成功但一篇都没收进来，和抓取压根没触发同步，在界面上原本长得一模一样；
    这条把每个库的漏斗摊开，让「今天到底收了什么」不用逐个点进任务详情才知道。
    """
    rows = await lab_service.daily_ingest_status(session, user)
    return [DailyIngestStatusRow(**row) for row in rows]


@router.get("/usage", response_model=list[UsageRow])
async def lab_usage(
    days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[UsageRow]:
    """全实验室最近 N 天的 token 用量（按 日期 × stage × model 聚合），全员可见。"""
    rows = await llm_admin_service.usage_report(session, days=days)
    return [UsageRow(**row) for row in rows]


@router.get("/usage/leaderboard", response_model=LabLeaderboardResponse)
async def lab_usage_leaderboard(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=10, ge=1, le=50),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> LabLeaderboardResponse:
    """成员 token 消耗排行榜。管理员关掉开关后，普通用户 403。"""
    enabled = await lab_service.get_leaderboard_enabled(session)
    if not enabled and user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="LEADERBOARD_DISABLED")
    rows = await lab_service.usage_leaderboard(session, days=days, limit=limit)
    return LabLeaderboardResponse(
        enabled=enabled, days=days, items=[LabLeaderboardEntry(**row) for row in rows]
    )


@router.get("/graph", response_model=GraphResponse)
async def lab_graph(
    library_id: uuid.UUID | None = Query(default=None, description="只看单个库；不传=全部可见库"),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> GraphResponse:
    """跨库知识图谱：全部可见库的并集（确定性构建，不走 LLM）。"""
    try:
        data = await lab_service.lab_graph(session, user=user, library_id=library_id)
    except lab_service.LibraryNotVisibleError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND") from exc
    return GraphResponse(**data)
