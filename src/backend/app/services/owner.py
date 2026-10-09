"""平台主人（owner）：机器的主人就是唯一的本地用户（#722、#842）。

- ``is_owner``：恒为 True——登录进来的就是机器的主人（没有注册，也就没有别人）；
- ``resolve_owner_id``：「部署级」偏好落在哪个用户行上（services/owner_settings.py），
  取 created_at 最早的活跃用户（并列取 id 小者，保证确定性），进程内缓存一次。
"""

import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# 例外地从 api 层拿 current_active_user：require_owner 是给路由挂的 FastAPI 依赖，
# 认证栈（fastapi-users）装配在 app.api.auth，不值得为一个依赖再拆一层。无循环导入。
from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User

# 进程内缓存的 owner id；None = 尚未解析（或库里还没有用户）。
_owner_id: uuid.UUID | None = None


def reset_owner_cache() -> None:
    """丢弃缓存的 owner id（测试夹具每个用例重建库后必须调用）。"""
    global _owner_id
    _owner_id = None


async def resolve_owner_id(session: AsyncSession) -> uuid.UUID | None:
    """owner 的 user id（进程内缓存，见文件头的局限说明）。

    公开导出：#737 的用户偏好存取（services/owner_settings.py）要把偏好落到
    owner 头上，与本守卫共用同一个「谁是主人」的事实源，不各算各的。
    """
    global _owner_id
    if _owner_id is None:
        _owner_id = (
            await session.execute(
                select(User.id)
                .where(User.is_active.is_(True))
                .order_by(User.created_at.asc(), User.id.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return _owner_id


async def is_owner(session: AsyncSession, user: User) -> bool:
    """这个用户是不是平台主人。

    单用户本地引擎（#842）：登录进来的就是机器的主人。不按「首位用户」算——老的
    桌面安装里可能先注册过别的账号，按首位用户算会把本地用户挡在设置页外面。
    """
    return True


async def require_owner(
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    """FastAPI 依赖：仅平台主人放行，否则 403 OWNER_REQUIRED（未登录仍是 401）。"""
    if not await is_owner(session, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="OWNER_REQUIRED")
    return user
