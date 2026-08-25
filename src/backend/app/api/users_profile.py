"""用户资料：头像上传/读取 + 本人 token 用量。"""

import io
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import block_read_only_personal_data, current_active_user
from app.core.config import get_settings
from app.core.db import get_session
from app.models.user import User
from app.schemas.llm_admin import UsageRow
from app.schemas.user import (
    ManagedCommandWatchdogUserRead,
    ManagedCommandWatchdogUserUpdate,
    UsageSummary,
    UsernameUpdate,
    UserRead,
    UserSearchResult,
)
from app.services import llm_admin as llm_admin_service
from app.services import managed_command_watchdog as watchdog_service
from app.services import users as users_service
from app.services.users import tokens_used_by_user

router = APIRouter(tags=["users"])


@router.get(
    "/users/me/managed-command-watchdog",
    response_model=ManagedCommandWatchdogUserRead,
)
async def get_my_managed_command_watchdog(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ManagedCommandWatchdogUserRead:
    admin_max = await watchdog_service.get_admin_max_minutes(session)
    own = watchdog_service.get_user_minutes(user, fallback=admin_max)
    return ManagedCommandWatchdogUserRead(
        unanswered_minutes=own,
        admin_max_unanswered_minutes=admin_max,
        effective_unanswered_minutes=min(own, admin_max),
    )


@router.put(
    "/users/me/managed-command-watchdog",
    response_model=ManagedCommandWatchdogUserRead,
)
async def set_my_managed_command_watchdog(
    payload: ManagedCommandWatchdogUserUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ManagedCommandWatchdogUserRead:
    own = await watchdog_service.set_user_minutes(session, user, payload.unanswered_minutes)
    admin_max = await watchdog_service.get_admin_max_minutes(session)
    return ManagedCommandWatchdogUserRead(
        unanswered_minutes=own,
        admin_max_unanswered_minutes=admin_max,
        effective_unanswered_minutes=min(own, admin_max),
    )


@router.patch("/users/me/username", response_model=UserRead)
async def set_my_username(
    body: UsernameUpdate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> User:
    """本人设置用户名：只能改一次（改后锁定），全局唯一。"""
    if user.username_locked:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="USERNAME_LOCKED")
    uname = body.username.lower()
    taken = (
        await session.execute(select(User.id).where(User.username == uname, User.id != user.id))
    ).first()
    if taken is not None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="USERNAME_TAKEN")
    user.username = uname
    user.username_locked = True
    await session.commit()
    await session.refresh(user)
    return user


# 注意：不能用 /users/search —— 会被 fastapi-users 的 /users/{id}（超管）路由抢占
@router.get("/collaborators/search", response_model=list[UserSearchResult])
async def search_users(
    q: str,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    _: User = Depends(block_read_only_personal_data),
) -> list[UserSearchResult]:
    """按 email/显示名模糊查平台用户（加协作者用）；排除自己。

    只读账号（游客）走不到这里：这个接口返回成员邮箱，藏了设置页的名册却留着它，
    等于换个入口把同一份东西交出去。"""
    rows = await users_service.search_users(session, q, limit=10, exclude_ids=[user.id])
    return [UserSearchResult(id=u.id, email=u.email, display_name=u.display_name) for u in rows]


_MAX_AVATAR_BYTES = 2 * 1024 * 1024
_AVATAR_SIZE = 256


def _avatar_dir() -> Path:
    d = Path(get_settings().data_dir) / "avatars"
    d.mkdir(parents=True, exist_ok=True)
    return d


@router.post("/users/me/avatar", response_model=UserRead)
async def upload_avatar(
    file: UploadFile,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> User:
    """上传头像：PNG/JPEG/WebP，≤2MB；统一裁方并缩到 256px PNG。"""
    raw = await file.read()
    if len(raw) > _MAX_AVATAR_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="AVATAR_TOO_LARGE")
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="AVATAR_NOT_IMAGE") from e
    # 中心裁方 + 缩放，统一存 PNG
    side = min(img.size)
    left = (img.width - side) // 2
    top = (img.height - side) // 2
    img = img.convert("RGBA").crop((left, top, left + side, top + side))
    img = img.resize((_AVATAR_SIZE, _AVATAR_SIZE), Image.LANCZOS)
    path = _avatar_dir() / f"{user.id}.png"
    img.save(path, format="PNG")
    user.avatar_path = str(path)
    await session.commit()
    await session.refresh(user)
    return user


@router.get("/users/{user_id}/avatar")
async def get_avatar(
    user_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _: User = Depends(current_active_user),
) -> FileResponse:
    target = await session.get(User, user_id)
    if target is None or not target.avatar_path or not Path(target.avatar_path).is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="AVATAR_NOT_FOUND")
    return FileResponse(target.avatar_path, media_type="image/png")


@router.get("/users/me/usage", response_model=UsageSummary)
async def my_usage(
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> UsageSummary:
    used = await tokens_used_by_user(session, user.id)
    return UsageSummary(tokens_used=used, token_quota=user.token_quota)


@router.get("/users/me/usage/history", response_model=list[UsageRow])
async def my_usage_history(
    days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[UsageRow]:
    """本人最近 N 天的用量（按 日期 × stage × model 聚合），供个人用量视图用。"""
    rows = await llm_admin_service.usage_report(session, user_id=user.id, days=days)
    return [UsageRow(**row) for row in rows]
