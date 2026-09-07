"""资源租约服务（R2 #677）：acquire / release + run 终态兜底释放。

语义：
- exclusive 资源：同一时刻最多一个活租约（capacity 忽略，按 1 算）；
- 非 exclusive：活租约数 < capacity 即可获（License 席位 / 队列并发额的计数语义）；
- wait=True 是「排队席位」的轮询实现——prepare 阶段可以等一会儿再开跑；真正的
  持久化排队表（席位可见、跨进程公平、断电不丢位）归后续 PR。

并发安全为什么这么做（而不是唯一约束或 BEGIN IMMEDIATE）：
- capacity>1 的计数语义没法用唯一约束表达，必须「数活租约 + 插入」原子化；
- postgres：SELECT … FOR UPDATE 锁资源行，两个并发 acquire 在行锁上串行，
  计数+插入压进同一事务，天然跨进程正确；
- sqlite（dev/测试）：没有行锁；async 引擎的连接池里对所有事务全局改
  BEGIN IMMEDIATE 副作用太大（每个只读事务都抢写锁）。这里用进程内
  asyncio.Lock 串行化临界区。**局限**：只护得住单进程——api 与 worker 两个
  进程同时 acquire 同一资源时仍有 check-then-insert 竞窗，只能靠 sqlite 库级
  单写者缩小窗口。生产环境是 postgres，不受此限。
"""

import asyncio
import time
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.resource import Resource, ResourceLease
from app.models.voyage import VoyageRun


class ResourceBusyError(Exception):
    """资源没有空闲席位（wait=False 直接抛；wait=True 等到超时也抛它）。"""


# sqlite 下串行化「数活租约 + 插入」的进程内锁（见模块 docstring 的 why 与局限）
_sqlite_acquire_lock = asyncio.Lock()


def effective_capacity(resource: Resource) -> int:
    """独占资源恒为 1；非独占取 capacity（防御脏数据，至少 1）。"""
    return 1 if resource.exclusive else max(1, resource.capacity)


async def count_active_leases(session: AsyncSession, resource_id: uuid.UUID) -> int:
    stmt = (
        select(func.count())
        .select_from(ResourceLease)
        .where(ResourceLease.resource_id == resource_id, ResourceLease.released_at.is_(None))
    )
    return int((await session.execute(stmt)).scalar_one())


def _is_postgres(session: AsyncSession) -> bool:
    return session.bind is not None and session.bind.dialect.name == "postgresql"


async def _try_acquire(
    session: AsyncSession,
    *,
    resource_id: uuid.UUID,
    capacity: int,
    run_id: uuid.UUID,
    note: str,
) -> ResourceLease | None:
    """一次获取尝试：事务内计数 + 插入；没有空位返回 None（不抛）。

    只收标量不收 ORM 对象：失败路径要 rollback（释放行锁、别拖住别人），
    rollback 会把会话里已加载的对象全部 expire，异步会话下再摸它们的属性会
    炸 MissingGreenlet——调用方在 rollback 前把要用的字段抄成标量最稳。
    """
    if _is_postgres(session):
        # 行锁串行化并发 acquire（锁到 commit/rollback 为止）
        await session.execute(
            select(Resource.id).where(Resource.id == resource_id).with_for_update()
        )
    if await count_active_leases(session, resource_id) >= capacity:
        await session.rollback()
        return None
    lease = ResourceLease(resource_id=resource_id, run_id=run_id, note=note)
    session.add(lease)
    await session.commit()
    await session.refresh(lease)
    return lease


async def _guarded_try_acquire(
    session: AsyncSession,
    *,
    resource_id: uuid.UUID,
    capacity: int,
    run_id: uuid.UUID,
    note: str,
) -> ResourceLease | None:
    kwargs = {"resource_id": resource_id, "capacity": capacity, "run_id": run_id, "note": note}
    if _is_postgres(session):
        return await _try_acquire(session, **kwargs)
    async with _sqlite_acquire_lock:
        return await _try_acquire(session, **kwargs)


async def acquire(
    session: AsyncSession,
    resource: Resource,
    run: VoyageRun,
    *,
    wait: bool = False,
    timeout: float = 300.0,
    note: str = "",
) -> ResourceLease:
    """给 run 获取一个资源席位。

    - wait=False：没有空位立刻抛 ResourceBusyError（调用方自己决定怎么办）；
    - wait=True：转轮询等位（wait_and_acquire），等到 timeout 还没有空位才抛。
    """
    label, resource_id = resource.name, resource.id  # rollback 前抄标量，见 _try_acquire
    capacity, run_id = effective_capacity(resource), run.id
    if not wait:
        lease = await _guarded_try_acquire(
            session, resource_id=resource_id, capacity=capacity, run_id=run_id, note=note
        )
        if lease is None:
            raise ResourceBusyError(f"resource {label} ({resource_id}) has no free slot")
        return lease
    return await wait_and_acquire(session, resource, run, timeout=timeout, note=note)


async def wait_and_acquire(
    session: AsyncSession,
    resource: Resource,
    run: VoyageRun,
    *,
    timeout: float = 300.0,
    poll_interval: float = 1.0,
    note: str = "",
) -> ResourceLease:
    """轮询版「排队席位」：反复尝试直到拿到或超时（prepare 阶段调用）。

    没有持久化队列 → 不保证先来先得（两个等位者谁先轮询到谁得）；
    这是本期的已知局限，排队表落地后此函数改为入队+等通知。
    """
    label, resource_id = resource.name, resource.id  # rollback 前抄标量，见 _try_acquire
    capacity, run_id = effective_capacity(resource), run.id
    deadline = time.monotonic() + timeout
    while True:
        lease = await _guarded_try_acquire(
            session, resource_id=resource_id, capacity=capacity, run_id=run_id, note=note
        )
        if lease is not None:
            return lease
        if time.monotonic() >= deadline:
            raise ResourceBusyError(
                f"resource {label} ({resource_id}) still busy after {timeout:.0f}s"
            )
        await asyncio.sleep(poll_interval)


async def release(session: AsyncSession, lease: ResourceLease) -> None:
    """释放租约（幂等：已释放的再释放是 no-op）。"""
    if lease.released_at is not None:
        return
    lease.released_at = utcnow()
    await session.commit()


async def release_for_run(session: AsyncSession, run_id: uuid.UUID) -> int:
    """run 终态兜底：一次释放该 run 的全部活租约，返回释放数（幂等）。

    正常路径应由 runner 的 cleanup 显式释放；这里是「不管怎么死的都不占着资源」
    的最后防线，挂在 voyage run 的终态写入点（engine._set_status / cancel_voyage /
    gates.fail_voyage）。
    """
    stmt = (
        update(ResourceLease)
        .where(ResourceLease.run_id == run_id, ResourceLease.released_at.is_(None))
        .values(released_at=utcnow())
    )
    result = await session.execute(stmt)
    await session.commit()
    return int(result.rowcount or 0)
