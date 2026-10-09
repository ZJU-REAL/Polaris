"""这台机器的偏好的读写（#737 配置分层）。

system_settings 曾把一批「其实是用户偏好」的键（抓取时刻、保留天数、TTS 全局档、
机构抽取模式等）存成平台全局单例。分层契约（docs/configuration.md）把这类键归还给
用户态：存 ``users.settings`` 的命名空间键（如 ``daily.sync_time``）。

**存到谁头上**：本地用户（services/local_user.py）——Polaris 是单用户本地应用，
这台机器只有这一个人。worker 的 cron 等没有请求上下文的读路径也能只凭 session
找到它。

**旧 system_settings 行**：#737 时读路径留过「新键缺席就回读旧行」的回退，说好只留
一期。#821 E2 把它删了：迁移 d3f9a1c7e2b4 先把旧行里还没有的值补到用户身上，
再删掉旧行。现在偏好只有一个家。
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.local_user import local_user_id


async def local_user(session: AsyncSession) -> User | None:
    """本地用户（User 行）；还没有用户时返回 None。"""
    user_id = await local_user_id(session)
    if user_id is None:
        return None
    return await session.get(User, user_id)


class NoOwnerError(RuntimeError):
    """还没有任何用户，偏好无处可存。

    以前这种时候退回写 system_settings 旧行；读路径的回退删掉之后那样写就是写丢，
    所以明说。实际不会发生：所有写入都来自已登录用户的请求（``user`` 必然有值）。
    """


async def read_setting(session: AsyncSession, key: str) -> Any:
    """读本地用户的命名空间偏好；没设过返回 None（各业务 getter 自带「缺省/非法回落默认」）。"""
    user = await local_user(session)
    if user is None:
        return None
    return (user.settings or {}).get(key)


async def write_setting(
    session: AsyncSession,
    key: str,
    value: Any,
    *,
    user: User | None = None,
) -> None:
    """把偏好写到本地用户的 settings 上（不 commit，沿用调用方的提交时机）。

    ``user`` 是发起操作的用户；库里还找不到本地用户时就落到他身上。
    """
    target = await local_user(session) or user
    if target is None:
        raise NoOwnerError(f"no user to store preference {key!r} on yet")
    # 整字典替换而不是就地改：JSON 列的变更检测认的是赋值
    target.settings = {**(target.settings or {}), key: value}
