"""管理端全局设置路由（仅 role=admin）：机构抽取模式、论文级向量总闸等。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.core.db import get_session
from app.schemas.admin_settings import (
    AffiliationModeRead,
    AffiliationModeUpdate,
    DailyEmbedBackfillResult,
    LabLeaderboardSettingRead,
    LabLeaderboardSettingUpdate,
    PaperEmbeddingRead,
    PaperEmbeddingUpdate,
)
from app.services import affiliations as affiliations_service
from app.services import daily_feed as daily_service
from app.services import lab as lab_service
from app.services import paper_enrich as paper_enrich_service

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


@router.get("/paper-embedding", response_model=PaperEmbeddingRead)
async def get_paper_embedding(session: AsyncSession = Depends(get_session)) -> PaperEmbeddingRead:
    """平台是否给论文建论文级向量（默认开，平台级总闸）。

    前身是 /daily-embed（默认关、只管每日推送）；默认关意味着推送来的论文在语义检索里
    根本搜不到，而只管一条路径也名不副实，故改名并扩到所有入库入口。"""
    return PaperEmbeddingRead(enabled=await paper_enrich_service.paper_embedding_enabled(session))


@router.put("/paper-embedding", response_model=PaperEmbeddingRead)
async def set_paper_embedding(
    payload: PaperEmbeddingUpdate,
    session: AsyncSession = Depends(get_session),
) -> PaperEmbeddingRead:
    return PaperEmbeddingRead(
        enabled=await paper_enrich_service.set_paper_embedding_enabled(session, payload.enabled)
    )


@router.post("/daily-embed/backfill", response_model=DailyEmbedBackfillResult)
async def backfill_daily_embed(
    session: AsyncSession = Depends(get_session),
) -> DailyEmbedBackfillResult:
    """给当前窗口内还没有向量的每日论文一次性补建（开开关后补齐历史用）。费用记系统账。"""
    stats = await daily_service.backfill_embeddings(session)
    return DailyEmbedBackfillResult(**stats)


@router.get("/lab-leaderboard", response_model=LabLeaderboardSettingRead)
async def get_lab_leaderboard(
    session: AsyncSession = Depends(get_session),
) -> LabLeaderboardSettingRead:
    """实验室概况页的用量排行榜是否对普通成员可见（默认开）。"""
    return LabLeaderboardSettingRead(enabled=await lab_service.get_leaderboard_enabled(session))


@router.put("/lab-leaderboard", response_model=LabLeaderboardSettingRead)
async def set_lab_leaderboard(
    payload: LabLeaderboardSettingUpdate,
    session: AsyncSession = Depends(get_session),
) -> LabLeaderboardSettingRead:
    return LabLeaderboardSettingRead(
        enabled=await lab_service.set_leaderboard_enabled(session, payload.enabled)
    )
