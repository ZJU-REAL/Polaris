"""向量空间：一批向量出自哪个模型、多少维（不 import fastapi）。

不同嵌入模型产出的向量落在互不相通的坐标系里，跨模型算余弦相似度没有意义——
维度不同时 postgres 会直接报错，**维度恰好相同时不报错，只是排序全乱**，后者才
是真正危险的：调用方一路成功返回，谁也看不出结果是噪声。

所以每条向量都带一个空间标识（``model@dim``），写入时打标、检索时过滤，让不同
空间的向量在物理上进不了同一次比较。同一时刻只有一个**激活空间**对检索可见。

维度不写死：平台第一次成功嵌入时，用模型实际返回的维度定义激活空间并存进
``system_settings``。换 4096 维的模型不需要改任何代码或建表语句——这正是本模块
存在的原因（见 issue #191）。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

# 激活空间：检索唯一认的空间，也是新向量的写入目标。未设置 = 平台还没建过任何向量。
ACTIVE_SPACE_SETTING_KEY = "embedding_active_space"
# 在建空间：后台正在预建的下一个空间（蓝绿切换用，本期只定义键，不消费）。
BUILDING_SPACE_SETTING_KEY = "embedding_building_space"


class EmbeddingSpaceMismatchError(RuntimeError):
    """嵌入结果与激活空间不符——绝不能入库，否则污染整个检索。"""


@dataclass(frozen=True)
class EmbeddingSpace:
    """一个向量坐标系。``key`` 是它在向量表里的标识。"""

    model: str
    dim: int

    @property
    def key(self) -> str:
        return f"{self.model}@{self.dim}"

    def __str__(self) -> str:  # 日志/报错里直接用
        return self.key


def _parse(value: object) -> EmbeddingSpace | None:
    """存量/脏值一律当作「没设置」，不抛错——设置读坏了不该让全站嵌入崩掉。"""
    if not isinstance(value, dict):
        return None
    model, dim = value.get("model"), value.get("dim")
    if not isinstance(model, str) or not model.strip():
        return None
    if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
        return None
    return EmbeddingSpace(model=model.strip(), dim=dim)


async def _read(session: AsyncSession, key: str) -> EmbeddingSpace | None:
    from app.models.system_setting import SystemSetting

    row = await session.get(SystemSetting, key)
    return _parse(row.value if row is not None else None)


async def active_space(session: AsyncSession) -> EmbeddingSpace | None:
    """当前对检索可见的空间；None = 平台还没建过向量（检索按「没有向量」处理）。

    不做缓存：这是一次主键查询，每次检索/每批嵌入才调一次，缓存换来的那点开销
    不值得担一份「管理员刚切了空间但旧值还在缓存里」的错乱风险。
    """
    return await _read(session, ACTIVE_SPACE_SETTING_KEY)


async def building_space(session: AsyncSession) -> EmbeddingSpace | None:
    """后台正在预建的空间（尚未对检索可见）。"""
    return await _read(session, BUILDING_SPACE_SETTING_KEY)


async def set_active_space(session: AsyncSession, space: EmbeddingSpace) -> None:
    """切换激活空间（调用方负责 commit）。

    切换本身只是一次设置写入：旧空间的向量行原封不动留在库里，切回去即可回滚。
    """
    await _write(session, ACTIVE_SPACE_SETTING_KEY, space)


async def set_building_space(
    session: AsyncSession, space: EmbeddingSpace | None
) -> None:
    """设置/清空在建空间（调用方负责 commit）。"""
    await _write(session, BUILDING_SPACE_SETTING_KEY, space)


async def _write(session: AsyncSession, key: str, space: EmbeddingSpace | None) -> None:
    from app.models.system_setting import SystemSetting

    value = None if space is None else {"model": space.model, "dim": space.dim}
    row = await session.get(SystemSetting, key)
    if row is None:
        session.add(SystemSetting(key=key, value=value))
    else:
        row.value = value
