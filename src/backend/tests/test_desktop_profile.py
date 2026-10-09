"""单进程本地引擎（#842 起唯一的形态）：内联任务队列、进程内 redis。"""

import asyncio

import pytest

from app.core.queue import InlineTaskQueue, UnregisteredTaskError, get_task_queue


async def test_the_task_queue_is_always_inline(monkeypatch):
    import app.core.queue as queue_mod

    monkeypatch.setattr(queue_mod, "_queue", None)
    assert isinstance(await get_task_queue(), InlineTaskQueue)
    monkeypatch.setattr(queue_mod, "_queue", None)


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


def test_redis_is_in_process(monkeypatch):
    import app.core.redis as redis_mod

    monkeypatch.setattr(redis_mod, "_client", None)
    client = redis_mod.get_redis()
    assert type(client).__module__.startswith("fakeredis")
    monkeypatch.setattr(redis_mod, "_client", None)
