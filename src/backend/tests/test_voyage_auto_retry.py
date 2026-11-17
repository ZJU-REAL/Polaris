"""临时性故障停住的任务自己过一会儿再试（1 / 5 / 20 分钟），三次不行才停着等人。"""

import asyncio
import uuid
from datetime import datetime, timedelta
from functools import partial

import httpx
import pytest

from app.agents.voyage import actions as actions_registry
from app.agents.voyage.engine import VoyageEngine
from app.agents.voyage.failures import AUTO_RETRY_DELAYS_MINUTES, schedule_auto_retry
from app.core import scheduler as sched
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.core.queue import InlineTaskQueue, _InlineArqRedis
from app.core.scheduler import PeriodicJob, Scheduler
from app.models.base import utcnow
from app.models.voyage import VoyageRun
from app.schemas.voyage import VoyageRead
from tests.conftest import RecordingBus, register_and_login
from worker.tasks import retry_paused_voyages


class _RecordingRedis:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, func: str, *args, **kwargs):
        self.jobs.append((func, args, kwargs))


class _Flaky:
    """一个可以开关的动作：坏着的时候抛指定异常，修好了返回产出。"""

    def __init__(self, exc: Exception) -> None:
        self.exc: Exception | None = exc
        self.calls = 0

    async def __call__(self, ctx, params):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return {"content": "ok"}


@pytest.fixture
def flaky(monkeypatch):
    def install(exc: Exception) -> _Flaky:
        action = _Flaky(exc)
        monkeypatch.setitem(actions_registry._REGISTRY, "test.flaky", action)
        return action

    return install


async def _project(client, email: str) -> tuple[str, dict]:
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "retry"}, headers=headers)
    return resp.json()["id"], headers


async def _pipeline_voyage(project_id: str, kind: str = "wiki_ingest") -> uuid.UUID:
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            project_id=uuid.UUID(project_id),
            kind=kind,
            mode="pipeline",
            goal="t",
            status="planning",
            plan=[{"title": "会出错的一步", "action": "test.flaky", "params": {}}],
        )
        session.add(run)
        await session.commit()
        return run.id


def _engine() -> VoyageEngine:
    return VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())


async def _load(run_id: uuid.UUID) -> VoyageRun:
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run is not None
        return run


def _due(run: VoyageRun) -> datetime:
    return datetime.fromisoformat(run.checkpoint["auto_retry"]["next_at"])


def test_schedule_is_one_five_twenty_minutes_then_stops():
    now = utcnow()
    checkpoint: dict = {}
    transient = {"transient": True}
    seen = []
    for _ in range(5):
        schedule_auto_retry(checkpoint, transient, now=now)
        seen.append((checkpoint["auto_retry"]["attempt"], checkpoint["auto_retry"]["next_at"]))
    assert AUTO_RETRY_DELAYS_MINUTES == (1, 5, 20)
    assert seen == [
        (1, (now + timedelta(minutes=1)).isoformat()),
        (2, (now + timedelta(minutes=5)).isoformat()),
        (3, (now + timedelta(minutes=20)).isoformat()),
        (3, None),
        (3, None),
    ]
    # 不是临时性的：不排；之前排着的也撤掉
    fresh: dict = {}
    schedule_auto_retry(fresh, {"transient": False}, now=now)
    assert "auto_retry" not in fresh
    pending = {"auto_retry": {"attempt": 1, "next_at": now.isoformat()}}
    schedule_auto_retry(pending, {"transient": False}, now=now)
    assert pending["auto_retry"] == {"attempt": 1, "next_at": None}


async def test_transient_failure_is_retried_three_times_then_stays_paused(
    client, queue_stub, flaky
):
    project_id, _headers = await _project(client, "retry-transient@example.com")
    action = flaky(httpx.ConnectError("connection refused"))
    run_id = await _pipeline_voyage(project_id)
    redis = _RecordingRedis()

    started = utcnow()
    await _engine().run(run_id)

    for attempt, minutes in enumerate(AUTO_RETRY_DELAYS_MINUTES, start=1):
        run = await _load(run_id)
        assert run.status == "paused_error"
        assert run.checkpoint["auto_retry"]["attempt"] == attempt
        due = _due(run)
        # 时钟有抖动，只卡区间：停下来之后 minutes 分钟
        assert timedelta(minutes=minutes, seconds=-5) < due - started
        assert due - utcnow() < timedelta(minutes=minutes, seconds=5)
        read = VoyageRead.model_validate(run)
        assert (read.retry_at, read.retry_attempt) == (due, attempt)

        # 没到点：不动
        assert await retry_paused_voyages({"redis": redis}, now=due - timedelta(seconds=1)) == []
        assert (await _load(run_id)).status == "paused_error"
        # 到点：拉起来，而且只拉一次
        assert await retry_paused_voyages({"redis": redis}, now=due) == [str(run_id)]
        assert await retry_paused_voyages({"redis": redis}, now=due + timedelta(hours=1)) == []
        assert len(redis.jobs) == attempt
        assert redis.jobs[-1][:2] == ("resume_voyage", (str(run_id),))
        assert redis.jobs[-1][2]["_job_id"] == f"voyage-{run_id}"
        run = await _load(run_id)
        assert run.status == "executing"
        read = VoyageRead.model_validate(run)
        assert (read.retry_at, read.retry_attempt) == (None, attempt)

        started = utcnow()
        await _engine().resume(run_id)  # 队列里那个 resume：还是连不上
        assert action.calls == attempt + 1

    # 三次用完：停着等人，不再排
    run = await _load(run_id)
    assert run.status == "paused_error"
    assert run.checkpoint["auto_retry"] == {"attempt": 3, "next_at": None}
    assert run.checkpoint["last_error"]["transient"] is True
    read = VoyageRead.model_validate(run)
    assert (read.retry_at, read.retry_attempt) == (None, 3)
    far = utcnow() + timedelta(days=1)
    assert await retry_paused_voyages({"redis": redis}, now=far) == []
    assert len(redis.jobs) == 3


async def test_permanent_failure_is_not_retried(client, queue_stub, flaky):
    project_id, _headers = await _project(client, "retry-permanent@example.com")
    action = flaky(ValueError("LLM 连续输出非法 JSON"))
    run_id = await _pipeline_voyage(project_id)
    redis = _RecordingRedis()

    await _engine().run(run_id)

    run = await _load(run_id)
    assert run.status == "paused_error"
    assert run.checkpoint["last_error"]["transient"] is False
    assert "auto_retry" not in run.checkpoint
    read = VoyageRead.model_validate(run)
    assert (read.retry_at, read.retry_attempt) == (None, 0)
    far = utcnow() + timedelta(days=1)
    assert await retry_paused_voyages({"redis": redis}, now=far) == []
    assert redis.jobs == [] and action.calls == 1


async def test_escaped_transient_error_is_scheduled_too(client, queue_stub):
    """没被步骤接住、由兜底停下来的（校验的模型调用超时）同样自动重试。"""
    project_id, headers = await _project(client, "retry-escaped@example.com")
    resp = await client.post(
        "/api/voyages",
        json={"kind": "demo", "project_id": project_id, "goal": "g"},
        headers=headers,
    )
    run_id = resp.json()["id"]
    router = LLMRouter()
    original = router.complete

    async def complete(stage, *args, **kwargs):
        if stage == "sextant":
            raise httpx.ReadTimeout("")
        return await original(stage, *args, **kwargs)

    router.complete = complete  # type: ignore[method-assign]
    await VoyageEngine(event_bus=RecordingBus(), llm_router=router).run(uuid.UUID(run_id))

    detail = (await client.get(f"/api/voyages/{run_id}", headers=headers)).json()
    assert detail["status"] == "paused_error"
    assert detail["retry_attempt"] == 1
    retry_at = datetime.fromisoformat(detail["retry_at"])
    assert timedelta(seconds=50) < retry_at - utcnow() < timedelta(seconds=70)


async def test_progress_after_a_retry_resets_the_schedule(client, queue_stub, flaky):
    project_id, _headers = await _project(client, "retry-reset@example.com")
    action = flaky(httpx.ReadTimeout(""))
    run_id = await _pipeline_voyage(project_id)
    redis = _RecordingRedis()

    await _engine().run(run_id)
    due = _due(await _load(run_id))
    action.exc = None  # 网络恢复
    assert await retry_paused_voyages({"redis": redis}, now=due) == [str(run_id)]
    await _engine().resume(run_id)

    run = await _load(run_id)
    assert run.status == "done"
    assert "auto_retry" not in run.checkpoint
    assert "last_error" not in run.checkpoint


async def test_schedule_survives_an_engine_restart(client, queue_stub, flaky):
    """重启前排好的重试，重启后照样到点执行：状态全在库里，内存里什么都不带。"""
    project_id, _headers = await _project(client, "retry-restart@example.com")
    action = flaky(httpx.ConnectError("connection refused"))
    run_id = await _pipeline_voyage(project_id)
    await _engine().run(run_id)
    due = _due(await _load(run_id))
    action.exc = None  # 重启期间网络恢复了

    # ---- 引擎重启：全新的队列和定时器，除了数据库没有任何东西延续下来 ----
    queue = InlineTaskQueue(max_voyages=1, max_jobs=1)

    def ctx() -> dict:
        return {"redis": _InlineArqRedis(queue)}

    # 定时器表里有这个任务，而且开机后很快就会先看一次（关机期间过点的在这时补上）
    job = next(j for j in sched.default_jobs() if j.name == "retry_paused_voyages")
    assert job.every_s <= 60 and job.first_after_s <= 60

    # 重启发生在到点之后：第一次触发就该把它拉起来
    after_restart = partial(retry_paused_voyages, now=due + timedelta(minutes=30))
    scheduler = Scheduler([PeriodicJob(job.name, 0.02, 0, after_restart)], ctx)
    scheduler.start()
    try:
        for _ in range(200):
            if (await _load(run_id)).status == "done":
                break
            await asyncio.sleep(0.02)
    finally:
        await scheduler.stop()
        await queue.drain()
        await queue.shutdown(0)

    run = await _load(run_id)
    assert run.status == "done"
    # 定时器转了很多圈，动作只多跑了一次
    assert scheduler.runs[job.name] > 1 and action.calls == 2
    assert "auto_retry" not in run.checkpoint


async def test_concurrent_ticks_resume_a_run_once(client, queue_stub, flaky):
    project_id, _headers = await _project(client, "retry-idempotent@example.com")
    flaky(httpx.ConnectError("connection refused"))
    run_id = await _pipeline_voyage(project_id)
    await _engine().run(run_id)
    due = _due(await _load(run_id))
    redis = _RecordingRedis()

    results = await asyncio.gather(
        *(retry_paused_voyages({"redis": redis}, now=due) for _ in range(4))
    )
    assert sorted(len(r) for r in results) == [0, 0, 0, 1]
    assert len(redis.jobs) == 1


async def test_a_live_driver_postpones_the_retry(client, queue_stub, flaky):
    project_id, _headers = await _project(client, "retry-busy@example.com")
    flaky(httpx.ConnectError("connection refused"))
    run_id = await _pipeline_voyage(project_id)
    await _engine().run(run_id)
    due = _due(await _load(run_id))

    class _Busy(_RecordingRedis):
        def is_running(self, job_id: str) -> bool:
            return True

    assert await retry_paused_voyages({"redis": _Busy()}, now=due) == []
    run = await _load(run_id)
    assert run.status == "paused_error" and _due(run) == due


async def test_manual_resume_clears_the_schedule(client, queue_stub, flaky):
    project_id, headers = await _project(client, "retry-manual@example.com")
    flaky(httpx.ConnectError("connection refused"))
    run_id = await _pipeline_voyage(project_id)
    await _engine().run(run_id)
    due = _due(await _load(run_id))

    detail = (await client.get(f"/api/voyages/{run_id}", headers=headers)).json()
    assert detail["retry_attempt"] == 1
    assert datetime.fromisoformat(detail["retry_at"]) == due
    assert detail["last_error"]["code"] == "NETWORK"

    resp = await client.post(f"/api/voyages/{run_id}/resume", headers=headers)
    assert resp.status_code == 200, resp.text
    assert (resp.json()["retry_at"], resp.json()["retry_attempt"]) == (None, 0)
    assert "auto_retry" not in (await _load(run_id)).checkpoint
    # 定时任务不会再插一脚
    redis = _RecordingRedis()
    assert await retry_paused_voyages({"redis": redis}, now=due + timedelta(hours=1)) == []
    assert redis.jobs == []


async def test_cancel_clears_the_schedule(client, queue_stub, flaky):
    project_id, headers = await _project(client, "retry-cancel@example.com")
    flaky(httpx.ConnectError("connection refused"))
    run_id = await _pipeline_voyage(project_id)
    await _engine().run(run_id)
    due = _due(await _load(run_id))

    resp = await client.post(f"/api/voyages/{run_id}/cancel", headers=headers)
    assert resp.status_code == 200, resp.text
    assert (resp.json()["retry_at"], resp.json()["retry_attempt"]) == (None, 0)
    run = await _load(run_id)
    assert run.status == "cancelled" and "auto_retry" not in run.checkpoint
    redis = _RecordingRedis()
    assert await retry_paused_voyages({"redis": redis}, now=due + timedelta(hours=1)) == []
    assert redis.jobs == [] and (await _load(run_id)).status == "cancelled"


async def test_retry_waits_while_another_idea_run_is_in_flight(client, queue_stub, flaky):
    project_id, _headers = await _project(client, "retry-idea@example.com")
    flaky(httpx.ConnectError("connection refused"))
    parked_id = await _pipeline_voyage(project_id, kind="idea_forge")
    await _engine().run(parked_id)
    due = _due(await _load(parked_id))
    async with get_sessionmaker()() as session:
        other = VoyageRun(
            project_id=uuid.UUID(project_id),
            kind="idea_review",
            mode="pipeline",
            goal="t",
            status="executing",
        )
        session.add(other)
        await session.commit()
        other_id = other.id
    redis = _RecordingRedis()

    # 同课题另一条 idea 任务在跑：不抢，重试时刻原样留着
    assert await retry_paused_voyages({"redis": redis}, now=due) == []
    run = await _load(parked_id)
    assert run.status == "paused_error" and _due(run) == due

    async with get_sessionmaker()() as session:
        other = await session.get(VoyageRun, other_id)
        other.status = "done"
        await session.commit()
    assert await retry_paused_voyages({"redis": redis}, now=due) == [str(parked_id)]
