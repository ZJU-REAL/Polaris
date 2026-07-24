"""管理端全局设置路由（仅 role=admin）：机构抽取模式、每日新论文自动建向量等。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.core.db import get_session
from app.schemas.admin_settings import (
    AffiliationModeRead,
    AffiliationModeUpdate,
    DailyEmbedBackfillResult,
    DailyEmbedRead,
    DailyEmbedUpdate,
)
from app.services import affiliations as affiliations_service
from app.services import daily_feed as daily_service

router = APIRouter(
    prefix="/admin/settings", tags=["admin-settings"], dependencies=[Depends(require_admin)]
)


@router.get("/affiliation-mode", response_model=AffiliationModeRead)
async def get_affiliation_mode(session: AsyncSession = Depends(get_session)) -> AffiliationModeRead:
    return AffiliationModeRead(
        mode=await affiliations_service.get_affiliation_extraction_mode(session)
    )


@router.put("/affiliation-mode", response_model=AffiliationModeRead)
async def set_affiliation_mode(
    payload: AffiliationModeUpdate,
    session: AsyncSession = Depends(get_session),
) -> AffiliationModeRead:
    try:
        mode = await affiliations_service.set_affiliation_extraction_mode(session, payload.mode)
    except affiliations_service.InvalidAffiliationModeError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"INVALID_AFFILIATION_MODE:{exc.mode}"
        ) from exc
    return AffiliationModeRead(mode=mode)


@router.get("/daily-embed", response_model=DailyEmbedRead)
async def get_daily_embed(session: AsyncSession = Depends(get_session)) -> DailyEmbedRead:
    """每日新论文是否自动建向量（关着时每日语义检索只能命中已建向量的论文）。"""
    return DailyEmbedRead(enabled=await daily_service.get_daily_embed_enabled(session))


@router.put("/daily-embed", response_model=DailyEmbedRead)
async def set_daily_embed(
    payload: DailyEmbedUpdate,
    session: AsyncSession = Depends(get_session),
) -> DailyEmbedRead:
    return DailyEmbedRead(
        enabled=await daily_service.set_daily_embed_enabled(session, payload.enabled)
    )


@router.post("/daily-embed/backfill", response_model=DailyEmbedBackfillResult)
async def backfill_daily_embed(
    session: AsyncSession = Depends(get_session),
) -> DailyEmbedBackfillResult:
    """给当前窗口内还没有向量的每日论文一次性补建（开开关后补齐历史用）。费用记系统账。"""
    stats = await daily_service.backfill_embeddings(session)
    return DailyEmbedBackfillResult(**stats)
