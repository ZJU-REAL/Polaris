"""本机唯一的本地用户（#842）。

Polaris 是单用户本地应用：会话只能经 ``/api/auth/local-session`` 取得，取到的永远是
``local@polaris.desktop`` 这一行。少数「这台机器的设置」（services/owner_settings.py
的偏好、LLM 路由读用量归属等）没有请求上下文可用，需要一个只凭 session 就能找到
这个人的办法——就是这里的 ``local_user_id``。

老安装库里可能还留着以前注册过的别的用户行：优先认本地用户的固定邮箱，找不到时
退回最早创建的活跃用户（created_at 并列按 id），保证结果确定。
"""

import uuid

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

# 与 app.api.auth.LOCAL_USER_EMAIL 同值。不从 api 层导入：services 不依赖 api。
LOCAL_USER_EMAIL = "local@polaris.desktop"


async def local_user_id(session: AsyncSession) -> uuid.UUID | None:
    """本地用户的 id；库里还没有任何活跃用户时返回 None。"""
    return (
        await session.execute(
            select(User.id)
            .where(User.is_active.is_(True))
            .order_by(
                case((User.email == LOCAL_USER_EMAIL, 0), else_=1),
                User.created_at.asc(),
                User.id.asc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
