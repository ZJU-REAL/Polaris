"""平台主人（owner）判定：admin 面只对「机器的主人」开放（#722，审计 #715 第 9 项）。

为什么需要它：治理字段（role 等）已随个人化定位删除（#614）——桌面档只有一个用户，
机器的主人不需要被自己治理。但 server 档 + 邀请码注册仍被支持（小团队自部署共用一台），
#616 把 require_admin 一刀切换成 current_active_user 后，任何注册用户都能改全局 LLM
密钥、文献源密钥、向量空间。这里恢复一个不需要任何新字段/迁移的最小守卫：

- desktop 档：恒为主人——唯一用户即机器的主人，不查库；
- server 档：主人 = created_at 最早的活跃用户（并列取 id 小者，保证确定性）。
  部署这台机器的人总是第一个注册的人，这个判据天然成立。

局限（有意为之）：
- owner id 进程内缓存一次：后续注册的用户不可能「挤掉」既有主人，也免得每个 admin
  请求都扫 users 表；代价是首用户被停用后，其他人要接管 admin 面得先动库再重启进程。
  owner 的显式转移/多 owner 属后续工作，不在本守卫范围。
"""

import uuid

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# 例外地从 api 层拿 current_active_user：require_owner 是给路由挂的 FastAPI 依赖，
# 认证栈（fastapi-users）装配在 app.api.auth，不值得为一个依赖再拆一层。无循环导入。
from app.api.auth import current_active_user
from app.core.config import get_settings
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
    """这个用户是不是平台主人（桌面档=本人；服务器档=首位用户）。"""
    if get_settings().is_desktop:
        return True
    owner_id = await resolve_owner_id(session)
    return owner_id is not None and user.id == owner_id


async def require_owner(
    user: User = Depends(current_active_user),
    session: AsyncSession = Depends(get_session),
) -> User:
    """FastAPI 依赖：仅平台主人放行，否则 403 OWNER_REQUIRED（未登录仍是 401）。"""
    if not await is_owner(session, user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="OWNER_REQUIRED")
    return user
