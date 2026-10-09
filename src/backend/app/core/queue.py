"""任务入队：引擎进程内直接执行（#842 起只有这一种形态）。

做成可注入依赖：测试覆盖 ``get_task_queue`` 为 stub（记录调用或内嵌直跑）。
"""

import asyncio
import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# 内联队列能执行的任务名，单一事实来源：队列按名字去 worker/tasks.py 取函数。
# 以前 ARQ 时代入队一个没注册的名字会被**静默丢弃**——#216 就是这么来的：库同步改成
# 事件驱动后 daily_wiki_ingest 从 cron 摘掉却没进注册表，每天入队、每天被丢，而抓取
# 步骤照报成功，连着几天没人发现。所以这里主动校验，让它当场炸掉而不是无声无息。
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
        "zotero_import",
        "full_export",
    }
)

# 驱动航程的任务：同一条航程任何时刻只能有一个驱动者。
VOYAGE_FUNCTIONS = frozenset({"run_voyage", "resume_voyage"})


def voyage_job_id(run_id: object) -> str:
    """一条航程的唯一任务 id——不管谁入队（API、对账、每日抓取），都落在这一个 id 上。

    以前对账用的是按时间分桶的 id、API 入队用的是自增 id，进程内去重互相认不出来：
    还在跑的长实验被对账判成「僵死」后又入队一个 resume，两个驱动者同时跑同一条航程，
    每 15 分钟再多一个（#850）。"""
    return f"voyage-{run_id}"


class UnregisteredTaskError(RuntimeError):
    """入队了一个没注册的任务名——直接拦下，不让它无声无息地什么也不做。"""


def check_task_name(name: str) -> None:
    if name not in WORKER_FUNCTIONS:
        raise UnregisteredTaskError(
            f"任务 {name!r} 不在 WORKER_FUNCTIONS 里，内联队列找不到它；"
            f"已注册的有：{sorted(WORKER_FUNCTIONS)}"
        )


class TaskQueue(Protocol):
    async def enqueue(self, func: str, *args: Any, **kwargs: Any) -> None: ...


class _InlineArqRedis:
    """worker 任务 ctx["redis"] 的替身。

    worker/tasks.py 里的任务把 ctx["redis"] 当 ArqRedis 用（历史接口）：既做普通 redis
    操作（EventBus pubsub 等），又调 ``enqueue_job`` 派生新任务。这里把属性访问代理到
    进程内 redis 客户端，把 ``enqueue_job`` 转回内联队列——派生任务同样内联执行。
    """

    def __init__(self, queue: "InlineTaskQueue") -> None:
        self._queue = queue

    async def enqueue_job(self, func: str, *args: Any, **kwargs: Any) -> None:
        await self._queue.enqueue(func, *args, **kwargs)

    def is_running(self, job_id: str) -> bool:
        return self._queue.is_running(job_id)

    def __getattr__(self, name: str) -> Any:
        from app.core.redis import get_redis

        return getattr(get_redis(), name)


class InlineTaskQueue:
    """任务在引擎进程内作为 asyncio 任务执行，机器上不需要 Redis/独立 worker。

    - 只接受 ``WORKER_FUNCTIONS`` 注册过的任务名（同一事实来源、当场炸掉）；
    - ``_job_id``：同 id 在途时不再起第二个（去重）；其余下划线参数忽略；
    - 航程任务（``VOYAGE_FUNCTIONS``）的 id 一律归一成 :func:`voyage_job_id`，调用方
      传什么都不算数——一条航程同一时刻只有一个驱动者。在途时又来一个 ``resume_voyage``
      （用户回答提问 / 批准闸门 / 重试，旧驱动者可能还在收尾）不丢弃，记成**一次**
      续跑，等在途那个结束后再起；带 ``_if_idle=True`` 的（对账）则直接丢弃；
    - 并发上限：航程与其它任务各一个信号量（``inline_max_voyages`` /
      ``inline_max_jobs``）。等名额的任务已经登记在册，照样参与去重、停机时照样被取消；
    - 定时任务不在这里：由引擎进程里的 core/scheduler.py 按间隔触发（#842）。
    """

    def __init__(self, *, max_voyages: int | None = None, max_jobs: int | None = None) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        #: 在途航程收尾后要再起的那一次续跑（每条航程最多记一次）
        self._followups: dict[str, tuple[str, tuple[Any, ...], dict[str, Any]]] = {}
        self._seq = 0
        self._closed = False
        self._voyage_slots = asyncio.Semaphore(max_voyages or settings.inline_max_voyages)
        self._job_slots = asyncio.Semaphore(max_jobs or settings.inline_max_jobs)

    async def enqueue(self, func: str, *args: Any, **kwargs: Any) -> None:
        check_task_name(func)
        job_id = kwargs.pop("_job_id", None)
        if_idle = bool(kwargs.pop("_if_idle", False))
        kwargs = {k: v for k, v in kwargs.items() if not k.startswith("_")}
        if self._closed:
            logger.warning("task queue is shutting down; dropped %s%s", func, args)
            return
        if func in VOYAGE_FUNCTIONS and args:
            job_id = voyage_job_id(args[0])
        if job_id is not None:
            existing = self._tasks.get(job_id)
            if existing is not None and not existing.done():
                if func == "resume_voyage" and not if_idle:
                    self._followups[job_id] = (func, args, kwargs)
                return  # 同 id 在途：去重
        else:
            self._seq += 1
            job_id = f"inline-{self._seq}"
        self._start(job_id, func, args, kwargs)

    def _start(self, job_id: str, func: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        from worker import tasks as worker_tasks  # 懒导入避免 app↔worker 环

        fn = getattr(worker_tasks, func)
        ctx = {"redis": _InlineArqRedis(self)}
        slots = self._voyage_slots if func in VOYAGE_FUNCTIONS else self._job_slots

        async def _run() -> Any:
            async with slots:
                return await fn(ctx, *args, **kwargs)

        task = asyncio.create_task(_run(), name=f"inline:{func}")
        self._tasks[job_id] = task
        task.add_done_callback(lambda t, jid=job_id: self._finish(jid, t))

    def _finish(self, job_id: str, task: asyncio.Task[Any]) -> None:
        if self._tasks.get(job_id) is task:
            self._tasks.pop(job_id)
        if not task.cancelled() and task.exception() is not None:
            logger.error("inline task %s failed", task.get_name(), exc_info=task.exception())
        followup = self._followups.pop(job_id, None)
        if followup is not None and not self._closed and job_id not in self._tasks:
            func, args, kwargs = followup
            try:
                self._start(job_id, func, args, kwargs)
            except RuntimeError:  # 事件循环已关：进程在退出，下次启动由对账接手
                logger.warning("could not start follow-up %s for %s", func, job_id)

    def is_running(self, job_id: str) -> bool:
        """该 id 的任务是否在途（含排队等名额）。"""
        task = self._tasks.get(job_id)
        return task is not None and not task.done()

    async def drain(self) -> None:
        """等待所有在途任务结束（测试用）。"""
        while self._tasks:
            pending = [t for t in self._tasks.values() if not t.done()]
            if pending:
                await asyncio.wait(pending)
            else:
                # 都已结束但完成回调（_finish：出列、起续跑）还没轮到：让出一拍
                await asyncio.sleep(0)

    async def shutdown(self, grace_s: float = 5.0) -> None:
        """停机：不再收新任务，取消所有在途任务，给它们 ``grace_s`` 秒收尾。

        必须在关数据库之前做——否则任务还在往一个已经 dispose 的 engine 里写。被打断的
        航程停在在途状态，下次启动由 reconcile_stuck_voyages 认领续跑。"""
        self._closed = True
        self._followups.clear()
        tasks = [t for t in self._tasks.values() if not t.done()]
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=grace_s)
        if pending:
            logger.warning(
                "%d inline task(s) did not stop within %.1fs: %s",
                len(pending),
                grace_s,
                sorted(t.get_name() for t in pending),
            )


_queue: TaskQueue | None = None


async def get_task_queue() -> TaskQueue:
    """FastAPI 依赖；测试覆盖为 stub（进程级单例）。"""
    global _queue
    if _queue is None:
        _queue = InlineTaskQueue()
    return _queue


async def shutdown_task_queue(grace_s: float = 5.0) -> None:
    """应用关闭时调用：取消并收掉进程内任务，然后丢弃单例。"""
    global _queue
    queue, _queue = _queue, None
    if isinstance(queue, InlineTaskQueue):
        await queue.shutdown(grace_s)
