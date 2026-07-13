"""ARQ 任务入队封装（API 进程侧）。

做成可注入依赖：测试覆盖 ``get_task_queue`` 为 stub（记录调用或内嵌直跑），
不依赖真实 Redis。
"""

from typing import Any, Protocol

from arq.connections import ArqRedis, RedisSettings, create_pool

from app.core.config import get_settings


class TaskQueue(Protocol):
    async def enqueue(self, func: str, *args: Any, **kwargs: Any) -> None: ...


class ArqTaskQueue:
    """懒初始化 ArqRedis 连接池并入队。"""

    def __init__(self) -> None:
        self._pool: ArqRedis | None = None

    async def _get_pool(self) -> ArqRedis:
        if self._pool is None:
            self._pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
        return self._pool

    async def enqueue(self, func: str, *args: Any, **kwargs: Any) -> None:
        pool = await self._get_pool()
        await pool.enqueue_job(func, *args, **kwargs)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
        self._pool = None


_queue = ArqTaskQueue()


async def get_task_queue() -> TaskQueue:
    """FastAPI 依赖；测试覆盖为 stub。"""
    return _queue
