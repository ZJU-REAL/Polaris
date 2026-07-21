"""管理员注册码管理：生成 / 列表 / 停用。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.core.db import get_session
from app.models.user import User
from app.schemas.registration_code import RegistrationCodeCreate, RegistrationCodeRead
from app.services import registration_codes as codes_service

router = APIRouter(prefix="/admin", tags=["admin-codes"])


def _to_read(rc) -> RegistrationCodeRead:
    return RegistrationCodeRead(
        id=rc.id,
        code=rc.code,
        note=rc.note,
        expires_at=rc.expires_at,
        max_uses=rc.max_uses,
        used_count=rc.used_count,
        revoked=rc.revoked,
        status=codes_service.code_status(rc),
        created_at=rc.created_at,
    )


@router.post(
    "/registration-codes",
    response_model=RegistrationCodeRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_registration_code(
    data: RegistrationCodeCreate,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> RegistrationCodeRead:
    rc = await codes_service.create_code(
        session,
        created_by=admin.id,
        note=data.note,
        expires_days=data.expires_days,
        max_uses=data.max_uses,
    )
    return _to_read(rc)


@router.get("/registration-codes", response_model=list[RegistrationCodeRead])
async def list_registration_codes(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> list[RegistrationCodeRead]:
    rows = await codes_service.list_codes(session)
    return [_to_read(rc) for rc in rows]


@router.delete(
    "/registration-codes/{code_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def revoke_registration_code(
    code_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> None:
    ok = await codes_service.revoke_code(session, code_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CODE_NOT_FOUND")
