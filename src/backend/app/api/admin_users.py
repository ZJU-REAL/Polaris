"""管理员用户管理：列表 / 新建 / 编辑 / 删除 / 批量删除 / 批量分配方向。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi_users import exceptions
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import UserManager, get_user_manager, require_admin
from app.core.db import get_session
from app.models.project import Project
from app.models.user import User
from app.schemas.project import ProjectRead
from app.schemas.user import (
    AdminUserCreate,
    AdminUserRead,
    AdminUserUpdate,
    BatchAssignRequest,
    BatchAssignResult,
    BatchDeleteRequest,
    BatchDeleteResult,
    UserCreate,
)
from app.services import users as users_service

router = APIRouter(prefix="/admin", tags=["admin-users"])


async def _read(session: AsyncSession, target: User) -> AdminUserRead:
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


@router.get("/users", response_model=list[AdminUserRead])
async def list_users(
    session: AsyncSession = Depends(get_session),
    _: User = Depends(require_admin),
) -> list[AdminUserRead]:
    rows = await users_service.list_users_with_usage(session)
    return [AdminUserRead(**r) for r in rows]


@router.post("/users", response_model=AdminUserRead, status_code=status.HTTP_201_CREATED)
async def create_user(
    data: AdminUserCreate,
    session: AsyncSession = Depends(get_session),
    user_manager: UserManager = Depends(get_user_manager),
    _: User = Depends(require_admin),
) -> AdminUserRead:
    """管理员直接建号（免邀请码），并设置角色 / LLM 权限 / 配额。"""
    if await users_service.username_taken(session, data.username):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="USERNAME_TAKEN")
    uc = UserCreate(
        email=data.email,
        password=data.password,
        display_name=data.display_name,
        username=data.username,
        invite_code="__admin_created__",  # 非表字段，create_update_dict 会剔除
    )
    try:
        target = await user_manager.create(uc, safe=True)
    except exceptions.UserAlreadyExists as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="EMAIL_TAKEN") from e
    except exceptions.InvalidPasswordException as e:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_PASSWORD", "reason": e.reason},
        ) from e
    # create 后应用管理员设定（role/llm_access/token_quota）
    target = await users_service.admin_update_user(
        session,
        target,
        {"role": data.role, "llm_access": data.llm_access, "token_quota": data.token_quota},
    )
    return await _read(session, target)


@router.patch("/users/{user_id}", response_model=AdminUserRead)
async def update_user(
    user_id: uuid.UUID,
    data: AdminUserUpdate,
    session: AsyncSession = Depends(get_session),
    user_manager: UserManager = Depends(get_user_manager),
    admin: User = Depends(require_admin),
) -> AdminUserRead:
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="USER_NOT_FOUND")
    payload = data.model_dump(exclude_unset=True)
    # 不能修改自己的角色/停用自己（防止管理员把自己锁在门外）
    if target.id == admin.id and ("role" in payload or "is_active" in payload):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="CANNOT_MODIFY_SELF_ROLE")
    # 重置密码：API 层用 password_helper 预先 hash，再交给 service
    if payload.pop("password", None) is not None:
        payload["hashed_password"] = user_manager.password_helper.hash(data.password)
    try:
        target = await users_service.admin_update_user(session, target, payload)
    except users_service.UsernameTakenError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="USERNAME_TAKEN") from e
    return await _read(session, target)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> None:
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="CANNOT_DELETE_SELF")
    target = await session.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="USER_NOT_FOUND")
    await users_service.delete_user(session, target)


@router.post("/users/batch-delete", response_model=BatchDeleteResult)
async def batch_delete_users(
    data: BatchDeleteRequest,
    session: AsyncSession = Depends(get_session),
    admin: User = Depends(require_admin),
) -> BatchDeleteResult:
    """批量删除（自动跳过自己）。"""
    deleted = await users_service.batch_delete_users(
        session, user_ids=data.user_ids, exclude_id=admin.id
    )
    return BatchDeleteResult(deleted=deleted)


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
