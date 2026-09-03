"""ARQ 任务入队封装（API 进程侧）。

做成可注入依赖：测试覆盖 ``get_task_queue`` 为 stub（记录调用或内嵌直跑），
不依赖真实 Redis。
"""

import asyncio
import logging
from typing import Any, Protocol

from arq.connections import ArqRedis, RedisSettings, create_pool

from app.core.config import get_settings

logger = logging.getLogger(__name__)

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
        "parse_paper_content_task",
        "daily_feed_sync",
        "daily_wiki_ingest",
        "daily_publication_match",
        "run_literature_discovery",
        "translate_literature_hit",
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


class _InlineArqRedis:
    """内联档位下 worker 任务 ctx["redis"] 的替身。

    worker/tasks.py 里的任务把 ctx["redis"] 当 ArqRedis 用：既做普通 redis 操作
    （EventBus pubsub 等），又调 ``enqueue_job`` 派生新任务。这里把属性访问代理到
    进程内 redis 客户端，把 ``enqueue_job`` 转回内联队列——派生任务同样内联执行。
    """

    def __init__(self, queue: "InlineTaskQueue") -> None:
        self._queue = queue

    async def enqueue_job(self, func: str, *args: Any, **kwargs: Any) -> None:
        await self._queue.enqueue(func, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        from app.core.redis import get_redis

        return getattr(get_redis(), name)


class InlineTaskQueue:
    """desktop 档位：任务在 API 进程内直接执行，机器上不需要 Redis/arq worker。

    语义对齐 arq 的最小子集：
    - 只接受 ``WORKER_FUNCTIONS`` 注册过的任务名（同一事实来源、同样当场炸掉）；
    - ``_job_id`` 保持「同 id 在途去重」语义（arq 用它防重复入队）；其余 arq 专属
      下划线参数（``_defer_by`` 等）忽略——desktop 单用户场景没有延迟投递需求；
    - cron 任务不在这里调度（桌面内核接管定时触发）。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._seq = 0

    async def enqueue(self, func: str, *args: Any, **kwargs: Any) -> None:
        check_task_name(func)
        job_id = kwargs.pop("_job_id", None)
        kwargs = {k: v for k, v in kwargs.items() if not k.startswith("_")}
        if job_id is not None:
            existing = self._tasks.get(job_id)
            if existing is not None and not existing.done():
                return  # 同 id 在途：对齐 arq 的去重语义
        else:
            self._seq += 1
            job_id = f"inline-{self._seq}"

        from worker import tasks as worker_tasks  # 懒导入避免 app↔worker 环

        fn = getattr(worker_tasks, func)
        ctx = {"redis": _InlineArqRedis(self)}
        task = asyncio.create_task(fn(ctx, *args, **kwargs), name=f"inline:{func}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda t, jid=job_id: self._finish(jid, t))

    def _finish(self, job_id: str, task: asyncio.Task[Any]) -> None:
        self._tasks.pop(job_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("inline task %s failed", task.get_name(), exc_info=task.exception())

    async def drain(self) -> None:
        """等待所有在途任务结束（测试与优雅停机用）。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks.values()), return_exceptions=True)


_queue: TaskQueue | None = None


async def get_task_queue() -> TaskQueue:
    """FastAPI 依赖；测试覆盖为 stub。按档位选择实现（进程级单例）。"""
    global _queue
    if _queue is None:
        _queue = InlineTaskQueue() if get_settings().is_desktop else ArqTaskQueue()
    return _queue
