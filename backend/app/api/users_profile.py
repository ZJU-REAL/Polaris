"""用户资料：头像上传/读取 + 本人 token 用量。"""

import io
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.config import get_settings
from app.core.db import get_session
from app.models.user import User
from app.schemas.user import UsageSummary, UsernameUpdate, UserRead, UserSearchResult
from app.services import users as users_service
from app.services.users import tokens_used_by_user

router = APIRouter(tags=["users"])


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
) -> list[UserSearchResult]:
    """按 email/显示名模糊查平台用户（加协作者用）；排除自己。"""
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
