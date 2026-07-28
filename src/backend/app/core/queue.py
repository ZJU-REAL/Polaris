"""ARQ 任务入队封装（API 进程侧）。

做成可注入依赖：测试覆盖 ``get_task_queue`` 为 stub（记录调用或内嵌直跑），
不依赖真实 Redis。
"""

from typing import Any, Protocol

from arq.connections import ArqRedis, RedisSettings, create_pool

from app.core.config import get_settings

# worker 实际注册在 WorkerSettings.functions 里的任务名，单一事实来源。
# arq 只执行注册过的函数，入队一个没注册的名字会被**静默丢弃**——#216 就是这么
# 来的：库同步改成事件驱动后 daily_wiki_ingest 从 cron 摘掉却没进 functions，
# 每天入队、每天被丢，而抓取步骤照报成功，连着几天没人发现。所以这里主动校验，
# 让它当场炸掉而不是无声无息。worker/settings.py 在导入时反向核对两边一致。
WORKER_FUNCTIONS = frozenset(
    {
        "ping_task",
        "run_voyage",
        "resume_voyage",
        "match_user_publications",
        "index_papers_fulltext_task",
        "daily_feed_sync",
        "daily_wiki_ingest",
        "daily_publication_match",
    }
)


class UnregisteredTaskError(RuntimeError):
    """入队了一个 worker 没注册的任务名——这会被 arq 静默丢弃，直接拦下。"""


def check_task_name(name: str) -> None:
    if name not in WORKER_FUNCTIONS:
        raise UnregisteredTaskError(
            f"任务 {name!r} 不在 WorkerSettings.functions 里，入队会被 arq 丢弃；"
            f"已注册的有：{sorted(WORKER_FUNCTIONS)}"
        )


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
        check_task_name(func)
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
