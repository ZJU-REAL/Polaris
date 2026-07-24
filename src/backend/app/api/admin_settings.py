"""管理端全局设置路由（仅 role=admin）：机构抽取模式等。"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.core.db import get_session
from app.schemas.admin_settings import AffiliationModeRead, AffiliationModeUpdate
from app.services import affiliations as affiliations_service

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
