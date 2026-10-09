"""引擎进程里的定时任务（#842 第 1 步）。

以前这些只挂在 ARQ worker 的 cron 上：每日论文抓取、文献发现的定时增量、发表匹配、
僵死航程回收、无人回答的 GPU 命令看门狗，外加 worker 启动时认领无人执行的航程。
桌面档位没有 ARQ worker，InlineTaskQueue 的注释说「桌面内核接管定时触发」——实际
没有任何代码这么做，于是桌面上这些从来没跑过：每日论文永远不来，崩溃后在途的航程
永远停在 executing。

这里用最朴素的方式补上：每个任务一个后台循环，按固定间隔调一次。任务自己判断
「到点没有、今天跑过没有」（它们本来就是按「cron 每 15 分钟空转一次」写的），所以
这里不需要 cron 表达式，间隔对齐原来的节奏即可。

- 同一个任务上一次还没跑完就不再起新的——不叠跑。
- 任务抛错只记日志，循环照转：一次网络抖动不该让每日论文从此停摆。
- 起步错开几秒：开机那一刻数据库、迁移、模型都还在热身。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("polaris.scheduler")

JobFn = Callable[[dict[str, Any]], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class PeriodicJob:
    name: str
    every_s: float
    #: 进程起来后多久跑第一次
    first_after_s: float
    fn: JobFn


def default_jobs() -> list[PeriodicJob]:
    """与原 ARQ cron 同一节奏（worker/settings.py）。懒导入：worker.tasks 依赖很重。"""
    from worker import tasks

    return [
        PeriodicJob(
            "dispatch_literature_discovery_schedules",
            300,
            40,
            tasks.dispatch_literature_discovery_schedules,
        ),
        PeriodicJob("daily_feed_sync", 900, 60, tasks.daily_feed_sync),
        PeriodicJob("daily_publication_match", 900, 90, tasks.daily_publication_match),
        PeriodicJob("reconcile_stale_voyages", 600, 120, tasks.reconcile_stale_voyages),
        PeriodicJob(
            "watch_unanswered_managed_commands", 300, 50, tasks.watch_unanswered_managed_commands
        ),
    ]


class Scheduler:
    def __init__(self, jobs: list[PeriodicJob], ctx_factory: Callable[[], dict[str, Any]]) -> None:
        self.jobs = jobs
        self._ctx_factory = ctx_factory
        self._loops: list[asyncio.Task[None]] = []
        #: 每个任务正在跑的那一次（防叠跑）
        self._running: dict[str, asyncio.Task[Any]] = {}
        self.runs: dict[str, int] = {}

    def start(self) -> None:
        if self._loops:
            return
        for job in self.jobs:
            self._loops.append(asyncio.create_task(self._loop(job), name=f"sched:{job.name}"))

    async def _loop(self, job: PeriodicJob) -> None:
        await asyncio.sleep(job.first_after_s)
        while True:
            self.trigger(job)
            await asyncio.sleep(job.every_s)

    def trigger(self, job: PeriodicJob) -> bool:
        """跑一次；上一次还在跑就跳过并返回 False。"""
        current = self._running.get(job.name)
        if current is not None and not current.done():
            return False
        task = asyncio.create_task(self._run(job), name=f"sched-run:{job.name}")
        self._running[job.name] = task
        return True

    async def _run(self, job: PeriodicJob) -> None:
        try:
            await job.fn(self._ctx_factory())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 一次失败不能让循环停摆
            logger.exception("scheduled job %s failed", job.name)
        finally:
            self.runs[job.name] = self.runs.get(job.name, 0) + 1

    async def stop(self) -> None:
        tasks = [*self._loops, *self._running.values()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
        self._loops.clear()
        self._running.clear()


_scheduler: Scheduler | None = None


async def start_engine_scheduler() -> Scheduler:
    """桌面档位启动时调用：起定时循环，并在后台认领崩溃前没跑完的航程。"""
    global _scheduler
    from app.core.queue import _InlineArqRedis, get_task_queue

    queue = await get_task_queue()

    def ctx() -> dict[str, Any]:
        # 与 InlineTaskQueue 给任务的 ctx 同形：任务里 ctx["redis"].enqueue_job(...) 入队
        return {"redis": _InlineArqRedis(queue)}

    _scheduler = Scheduler(default_jobs(), ctx)
    _scheduler.start()
    from worker.tasks import reconcile_stuck_voyages

    # 原来是 worker 的 on_startup。放进一次性任务而不是在启动路径上 await：
    # 对账出错不该拦住整个引擎起来
    _scheduler.trigger(PeriodicJob("reconcile_stuck_voyages", 0, 0, reconcile_stuck_voyages))
    return _scheduler


async def stop_engine_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        await _scheduler.stop()
        _scheduler = None


__all__ = [
    "PeriodicJob",
    "Scheduler",
    "default_jobs",
    "start_engine_scheduler",
    "stop_engine_scheduler",
]
