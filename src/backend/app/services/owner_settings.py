"""用户偏好的归属解析与读写（#737 配置分层）。

system_settings 曾把一批「其实是用户偏好」的键（每日订阅分类、抓取时刻、保留天数、
TTS 全局档、机构抽取模式等）存成平台全局单例——单用户产品里这没毛病，但它让
「谁的偏好」这个问题没有答案：server 档多个账号共用一份、谁改都生效。分层契约
（docs/configuration.md）把这类键归还给用户态：存 ``users.settings`` 的命名空间键
（如 ``daily.categories``）。

**存到谁头上**：这批偏好驱动的是部署级行为（每日池全部署共享一份、TTS 走同一个
上游），所以规范落点是 **owner 用户**——desktop 单用户就是本人；server 档取最早
注册的活跃用户。判定复用 #722 的 services/owner.py（admin 门禁与偏好落点共用同一个
「谁是主人」事实源，含其进程内缓存语义：首用户被停用不会在进程内换主）。

**迁移期回退**：alembic 数据迁移会把存量 system_settings 拷进 owner 的 settings；
读路径在新键缺席时仍回读旧行（deprecated，保留一期），确保备份回滚/漏拷的部署
不丢配置。写路径只写新位置，旧行不再更新。
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_setting import SystemSetting
from app.models.user import User
from app.services.owner import resolve_owner_id

_MISSING = object()


async def owner_user(session: AsyncSession) -> User | None:
    """部署的主人（User 行）；还没有用户时返回 None。

    判定走 services/owner.resolve_owner_id（#722 同源：最早注册的活跃用户，
    created_at 并列按 id 决出稳定次序；含进程内缓存）。
    """
    owner_id = await resolve_owner_id(session)
    if owner_id is None:
        return None
    return await session.get(User, owner_id)


async def read_setting(session: AsyncSession, key: str, *, legacy_key: str | None) -> Any:
    """读 owner 的命名空间偏好；新键缺席时回读旧 system_settings 行。

    回退只此一期（deprecated）：数据迁移已把存量拷进 owner.settings，这里兜的是
    「迁移后才从备份恢复出旧行」之类的残局。下一期删回退时连 legacy 行一起清。
    取值合法性不在这里管——各业务 getter 自带「非法回落默认」。

    ``legacy_key=None``：这个键是本期新增的，没有旧行可回读——不该逼着新键
    去编一个从来不存在的 legacy 名字。
    """
    owner = await owner_user(session)
    if owner is not None:
        value = (owner.settings or {}).get(key, _MISSING)
        if value is not _MISSING:
            return value
    row = await session.get(SystemSetting, legacy_key or key)
    return row.value if row is not None else None


async def write_setting(
    session: AsyncSession,
    key: str,
    value: Any,
    *,
    legacy_key: str | None,
    user: User | None = None,
) -> None:
    """把偏好写到 owner 的 settings 上（不 commit，沿用调用方的提交时机）。

    ``user`` 是发起操作的用户：desktop 单用户 = owner = 本人；server 档上非 owner
    的写入也落到 owner（这批偏好是部署级行为，只有一份真相；#722 合并后 /admin
    面会把非 owner 挡在 API 层）。库里还没有活跃用户时退回写旧行——read_setting
    的回退仍能读到它，等首个用户注册后由使用方自然写到新位置。
    """
    target = await owner_user(session) or user
    if target is not None:
        # 整字典替换而不是就地改：JSON 列的变更检测认的是赋值
        target.settings = {**(target.settings or {}), key: value}
        return
    # 还没有任何用户时仍要落盘：写丢了就是「设了订阅、下次打开没了」。
    # 本期新增的键没有 legacy 名字，就用键名自己当行名——回退行的意义是
    # 「别丢」，不是「必须叫某个历史名字」。
    row_key = legacy_key or key
    row = await session.get(SystemSetting, row_key)
    if row is None:
        session.add(SystemSetting(key=row_key, value=value))
    else:
        row.value = value
