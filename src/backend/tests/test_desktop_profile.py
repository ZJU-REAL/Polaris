"""desktop 档位（POLARIS_PROFILE=desktop）：单进程、内联任务队列、进程内 redis。"""

import asyncio

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.queue import InlineTaskQueue, UnregisteredTaskError


def test_desktop_profile_forbids_prod_env():
    with pytest.raises(ValidationError):
        Settings(profile="desktop", env="prod")


def test_desktop_profile_flag():
    assert Settings(profile="desktop").is_desktop
    # 显式钉 server：整套测试可能在 POLARIS_PROFILE=desktop 下运行（A2 验收方式）。
    assert not Settings(profile="server").is_desktop


@pytest.mark.asyncio
async def test_inline_queue_executes_registered_task_in_process():
    queue = InlineTaskQueue()
    # ping_task 是注册表里最小的真实任务：内联执行完成即证明
    # 「入队 → 进程内跑 worker 协程」这条链是通的。
    await queue.enqueue("ping_task", "desktop")
    await queue.drain()
    assert queue._tasks == {}


@pytest.mark.asyncio
async def test_inline_queue_rejects_unregistered_task():
    queue = InlineTaskQueue()
    with pytest.raises(UnregisteredTaskError):
        await queue.enqueue("no_such_task")


@pytest.mark.asyncio
async def test_inline_queue_deduplicates_by_job_id(monkeypatch):
    import worker.tasks as worker_tasks

    started = []
    release = asyncio.Event()

    async def slow_task(ctx, message="x"):
        started.append(message)
        await release.wait()

    monkeypatch.setattr(worker_tasks, "ping_task", slow_task)
    queue = InlineTaskQueue()
    await queue.enqueue("ping_task", "a", _job_id="same")
    await queue.enqueue("ping_task", "b", _job_id="same")  # 在途同 id：丢弃
    await asyncio.sleep(0)
    release.set()
    await queue.drain()
    assert started == ["a"]


@pytest.mark.asyncio
async def test_inline_ctx_redis_reenqueues_inline(monkeypatch):
    """任务经 ctx["redis"].enqueue_job 派生的新任务同样内联执行。"""
    import worker.tasks as worker_tasks

    ran = []

    async def parent(ctx, message="x"):
        ran.append(f"parent:{message}")
        await ctx["redis"].enqueue_job("match_user_publications", "child")

    async def child(ctx, message="x"):
        ran.append(f"child:{message}")

    queue = InlineTaskQueue()
    monkeypatch.setattr(worker_tasks, "ping_task", parent)
    monkeypatch.setattr(worker_tasks, "match_user_publications", child)
    await queue.enqueue("ping_task", "root")
    await queue.drain()
    assert ran == ["parent:root", "child:child"]


def test_desktop_redis_is_in_process(monkeypatch):
    import app.core.redis as redis_mod

    monkeypatch.setattr(redis_mod, "_client", None)
    monkeypatch.setattr(
        "app.core.redis.get_settings", lambda: Settings(profile="desktop")
    )
    client = redis_mod.get_redis()
    assert type(client).__module__.startswith("fakeredis")
    monkeypatch.setattr(redis_mod, "_client", None)
