"""管理员用户管理：列表（含用量）/ 编辑（角色、配额、功能权限）/ 批量分配方向。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import require_admin
from app.core.db import get_session
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectRead
from app.schemas.user import (
    AdminUserRead,
    AdminUserUpdate,
    BatchAssignRequest,
    BatchAssignResult,
)
from app.services import users as users_service

router = APIRouter(prefix="/admin", tags=["admin-users"])


@router.get("/users", response_model=list[AdminUserRead])
async def list_users(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> list[AdminUserRead]:
    rows = await users_service.list_users_with_usage(session)
    return [AdminUserRead(**r) for r in rows]


@router.patch("/users/{user_id}", response_model=AdminUserRead)
async def update_user(
    user_id: uuid.UUID,
    data: AdminUserUpdate,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> AdminUserRead:
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="USER_NOT_FOUND")
    payload = data.model_dump(exclude_unset=True)
    # 不能修改自己的角色/停用自己（防止管理员把自己锁在门外）
    if target.id == admin.id and ("role" in payload or "is_active" in payload):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="CANNOT_MODIFY_SELF_ROLE")
    target = await users_service.admin_update_user(session, target, payload)
    tokens_used = await users_service.tokens_used_by_user(session, target.id)
    return AdminUserRead(
        id=target.id,
        email=target.email,
        display_name=target.display_name,
        username=target.username,
        role=target.role,
        is_active=target.is_active,
        has_avatar=target.has_avatar,
        llm_access=target.llm_access,
        llm_self_managed=target.llm_self_managed,
        token_quota=target.token_quota,
        features=target.features,
        tokens_used=tokens_used,
        created_at=target.created_at,
    )


@router.get("/projects", response_model=list[ProjectRead])
async def list_all_projects(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> list[ProjectRead]:
    """全部研究方向（批量分配用）。"""
    projects = (await session.execute(select(Project).order_by(Project.created_at))).scalars().all()
    return [ProjectRead.model_validate(p) for p in projects]


@router.post("/users/batch-assign", response_model=BatchAssignResult)
async def batch_assign(
    data: BatchAssignRequest,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> BatchAssignResult:
    added = await users_service.batch_assign(
        session, user_ids=data.user_ids, project_ids=data.project_ids, role=data.role
    )
    return BatchAssignResult(added=added)
