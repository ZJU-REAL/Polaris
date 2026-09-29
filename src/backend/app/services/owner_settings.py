"""用户偏好的归属解析与读写（#737 配置分层）。

system_settings 曾把一批「其实是用户偏好」的键（每日订阅分类、抓取时刻、保留天数、
TTS 全局档、机构抽取模式等）存成平台全局单例——单用户产品里这没毛病，但它让
「谁的偏好」这个问题没有答案：server 档多个账号共用一份、谁改都生效。分层契约
（docs/configuration.md）把这类键归还给用户态：存 ``users.settings`` 的命名空间键
（如 ``daily.sync_time``）。

**存到谁头上**：本模块管的这批偏好驱动的是**部署级**行为（一个 cron、一份存储预算、
TTS 走同一个上游），所以规范落点是 **owner 用户**——desktop 单用户就是本人；server
档取最早注册的活跃用户。判定复用 #722 的 services/owner.py（admin 门禁与偏好落点
共用同一个「谁是主人」事实源，含其进程内缓存语义：首用户被停用不会在进程内换主）。

**不归这里管的**：真正因人而异的偏好不走这套。每日订阅（``daily.categories`` /
``daily.subscriptions``）自 #806 起存在各人自己的 ``users.settings`` 上，由
services/daily_feed.py 直接读写，且**不回退到 owner**——回退正是「新注册的人继承
第一个注册者的领域」那个 bug。加新键之前先问一句：它是这台机器的属性，还是某个
人的偏好。

**旧 system_settings 行**：#737 时读路径留过「新键缺席就回读旧行」的回退，说好只留
一期。#821 E2 把它删了：迁移 d3f9a1c7e2b4 先把旧行里 owner 还没有的值补到 owner 身上，
再删掉旧行。现在偏好只有一个家。
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.owner import resolve_owner_id


async def owner_user(session: AsyncSession) -> User | None:
    """部署的主人（User 行）；还没有用户时返回 None。

    判定走 services/owner.resolve_owner_id（#722 同源：最早注册的活跃用户，
    created_at 并列按 id 决出稳定次序；含进程内缓存）。
    """
    owner_id = await resolve_owner_id(session)
    if owner_id is None:
        return None
    return await session.get(User, owner_id)


class NoOwnerError(RuntimeError):
    """还没有任何用户，偏好无处可存。

    以前这种时候退回写 system_settings 旧行；读路径的回退删掉之后那样写就是写丢，
    所以明说。实际不会发生：所有写入都来自已登录用户的请求（``user`` 必然有值）。
    """


async def read_setting(session: AsyncSession, key: str) -> Any:
    """读 owner 的命名空间偏好；没设过返回 None（各业务 getter 自带「缺省/非法回落默认」）。"""
    owner = await owner_user(session)
    if owner is None:
        return None
    return (owner.settings or {}).get(key)


async def write_setting(
    session: AsyncSession,
    key: str,
    value: Any,
    *,
    user: User | None = None,
) -> None:
    """把偏好写到 owner 的 settings 上（不 commit，沿用调用方的提交时机）。

    ``user`` 是发起操作的用户：desktop 单用户 = owner = 本人；server 档上非 owner
    的写入也落到 owner（这批偏好是部署级行为，只有一份真相；非 owner 在 API 层
    已被挡住）。库里还没有 owner 时落到 ``user`` 身上——他注册后就是 owner。
    """
    target = await owner_user(session) or user
    if target is None:
        raise NoOwnerError(f"no user to store preference {key!r} on yet")
    # 整字典替换而不是就地改：JSON 列的变更检测认的是赋值
    target.settings = {**(target.settings or {}), key: value}
