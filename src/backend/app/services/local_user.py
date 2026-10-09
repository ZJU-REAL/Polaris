"""本机唯一的本地用户（#842）。

Polaris 是单用户本地应用：会话只能经 ``/api/auth/local-session`` 取得，取到的永远是
``local@polaris.desktop`` 这一行。少数「这台机器的设置」（services/owner_settings.py
的偏好、LLM 路由读用量归属等）没有请求上下文可用，需要一个只凭 session 就能找到
这个人的办法——就是这里的 ``local_user_id``。

「本地用户」的认法（与迁移 7d2e4f9a1b63 合并用户行、3c7d9e1f5a20 并 LLM 配置同一条
规则，#850）：优先认固定邮箱；找不到时退回最早创建的活跃用户（created_at 并列按
id），保证结果确定。``ensure_local_user`` 在引擎启动时和 local-session 里各调一次：
没有 local@ 时把那位最早的用户认作本地用户（改邮箱），库里一个人都没有才新建——
这样定时任务从第一拍起看到的就是同一个人。
"""

import secrets
import uuid

from fastapi_users.password import PasswordHelper
from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

# 与 app.api.auth.LOCAL_USER_EMAIL 同值。不从 api 层导入：services 不依赖 api。
LOCAL_USER_EMAIL = "local@polaris.desktop"
LOCAL_USERNAME = "local"


def _canonical_order():
    return (
        case((User.email == LOCAL_USER_EMAIL, 0), else_=1),
        User.created_at.asc(),
        User.id.asc(),
    )


async def local_user_id(session: AsyncSession) -> uuid.UUID | None:
    """本地用户的 id；库里还没有任何活跃用户时返回 None。"""
    return (
        await session.execute(
            select(User.id).where(User.is_active.is_(True)).order_by(*_canonical_order()).limit(1)
        )
    ).scalar_one_or_none()


async def _free_username(session: AsyncSession, exclude: uuid.UUID | None = None) -> str | None:
    """``local``，被占了就 ``local_2``、``local_3``……（用户名只许小写字母/数字/下划线）；
    实在都被占了返回 None（列可空）。"""
    for n in range(1, 100):
        candidate = LOCAL_USERNAME if n == 1 else f"{LOCAL_USERNAME}_{n}"
        stmt = select(User.id).where(User.username == candidate)
        if exclude is not None:
            stmt = stmt.where(User.id != exclude)
        if (await session.execute(stmt.limit(1))).first() is None:
            return candidate
    return None


async def _find_canonical(session: AsyncSession) -> User | None:
    user = (
        await session.execute(select(User).where(User.email == LOCAL_USER_EMAIL))
    ).scalar_one_or_none()
    if user is not None:
        return user
    return (
        await session.execute(
            select(User)
            .order_by(User.is_active.desc(), User.created_at.asc(), User.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def ensure_local_user(session: AsyncSession) -> User:
    """返回本地用户，必要时把它建出来或认领下来；幂等。

    - 有 ``local@polaris.desktop``：就是它（停用了就重新启用——它是唯一能拿会话的人）；
    - 没有但库里有人：最早的活跃用户改成这个邮箱（用户名 ``local`` 没人占才改），
      于是 local-session 接上它的数据，而不是另建一个空用户；
    - 一个人都没有：新建。
    """
    for attempt in range(2):
        user = await _find_canonical(session)
        changed = False
        if user is None:
            user = User(
                email=LOCAL_USER_EMAIL,
                hashed_password=PasswordHelper().hash(secrets.token_urlsafe(32)),
                is_active=True,
                is_superuser=False,
                is_verified=True,
                display_name="Local",
                username=await _free_username(session),
            )
            session.add(user)
            changed = True
        else:
            if user.email != LOCAL_USER_EMAIL:
                user.email = LOCAL_USER_EMAIL
                if await _free_username(session, exclude=user.id) == LOCAL_USERNAME:
                    user.username = LOCAL_USERNAME
                changed = True
            if not user.is_active:
                user.is_active = True
                changed = True
        if not changed:
            return user
        try:
            await session.commit()
        except IntegrityError:
            # 并发的另一个请求先建出了 local@：回滚后再找一次就是它
            await session.rollback()
            if attempt:
                raise
            continue
        await session.refresh(user)
        return user
    raise RuntimeError("unreachable")
