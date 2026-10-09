"""引擎进程里的定时任务（#842）：桌面没有 ARQ worker，原来那些 cron 由它来跑。"""

import asyncio

from app.core import scheduler as sched
from app.core.scheduler import PeriodicJob, Scheduler


async def test_jobs_run_repeatedly_and_survive_failures():
    calls: list[str] = []

    async def ok(ctx):
        calls.append("ok")

    async def boom(ctx):
        calls.append("boom")
        raise RuntimeError("network blip")

    s = Scheduler([PeriodicJob("ok", 0.02, 0, ok), PeriodicJob("boom", 0.02, 0, boom)], lambda: {})
    s.start()
    await asyncio.sleep(0.15)
    await s.stop()
    # 失败的那个也一直在被调：一次出错不能让它从此停摆
    assert calls.count("ok") >= 3 and calls.count("boom") >= 3


async def test_a_slow_job_is_never_stacked():
    running = 0
    peak = 0

    async def slow(ctx):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.1)
        running -= 1

    job = PeriodicJob("slow", 0.01, 0, slow)
    s = Scheduler([job], lambda: {})
    s.start()
    await asyncio.sleep(0.05)
    assert s.trigger(job) is False  # 上一次还在跑
    await asyncio.sleep(0.2)
    await s.stop()
    assert peak == 1


def test_same_jobs_as_the_worker_cron():
    """与服务器档位 worker 的 cron 表一一对应：漏一个，桌面上那件事就永远不发生。"""
    from worker.settings import WorkerSettings

    cron = {c.coroutine.__name__ for c in WorkerSettings.cron_jobs}
    assert {j.fn.__name__ for j in sched.default_jobs()} == cron
    assert WorkerSettings.on_startup.__name__ == "reconcile_stuck_voyages"


async def test_engine_start_reclaims_stuck_runs_once(monkeypatch):
    seen: list[dict] = []

    async def fake_reconcile(ctx):
        seen.append(ctx)

    import worker.tasks

    monkeypatch.setattr(worker.tasks, "reconcile_stuck_voyages", fake_reconcile)
    monkeypatch.setattr(sched, "default_jobs", lambda: [])
    s = await sched.start_engine_scheduler()
    await asyncio.sleep(0.05)
    await sched.stop_engine_scheduler()
    assert len(seen) == 1
    # 任务拿到的 ctx 能入队（对账就是靠它把航程重新排上）
    assert hasattr(seen[0]["redis"], "enqueue_job")
    assert s.runs.get("reconcile_stuck_voyages") == 1
