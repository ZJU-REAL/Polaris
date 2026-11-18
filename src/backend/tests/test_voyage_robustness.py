"""任务不会悬着：漏出来的异常一律停到 paused_error 并写明原因；对账认领有次数上限。"""

import sqlite3
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.agents.voyage import actions as actions_registry
from app.agents.voyage.engine import VoyageEngine, park_run
from app.agents.voyage.failures import PreconditionError, classify_failure
from app.agents.voyage.helm import Helm
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.base import utcnow
from app.models.voyage import VoyageRun, VoyageStep
from app.schemas.voyage import VoyageRead
from tests.conftest import RecordingBus, register_and_login
from worker import tasks as worker_tasks
from worker.tasks import (
    MAX_RECLAIMS_WITHOUT_PROGRESS,
    reconcile_stale_voyages,
    reconcile_stuck_voyages,
)

# ---- 归类 ----


class _Typed(Exception):
    """并行在做的类型化模型错误的样子：自己声明 code / transient / user_message。"""

    def __init__(self, code: str, transient: bool, user_message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient
        self.user_message = user_message


class _WithStatus(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class LLMRefusalError(RuntimeError):
    pass


class LLMNotConfiguredError(RuntimeError):
    pass


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/x")
    return httpx.HTTPStatusError(
        "boom", request=request, response=httpx.Response(status_code, request=request)
    )


def _wrapped(inner: BaseException) -> BaseException:
    try:
        raise ValueError("step failed") from inner
    except ValueError as e:
        return e


@pytest.mark.parametrize(
    ("exc", "code", "transient"),
    [
        # 异常自己声明的最优先，哪怕类型看起来像别的
        (_Typed("LLM_OVERLOADED", True), "LLM_OVERLOADED", True),
        (_Typed("LLM_SCHEMA", False), "LLM_SCHEMA", False),
        # 网络 / 超时
        (httpx.ConnectError("refused"), "NETWORK", True),
        (httpx.ReadTimeout(""), "TIMEOUT", True),
        (httpx.RemoteProtocolError("peer closed"), "NETWORK", True),
        (TimeoutError(), "TIMEOUT", True),
        (ConnectionResetError(104, "reset"), "NETWORK", True),
        (OSError(110, "timed out"), "TIMEOUT", True),
        (OSError(113, "no route to host"), "NETWORK", True),
        # HTTP 状态码
        (_http_error(429), "RATE_LIMITED", True),
        (_http_error(500), "UPSTREAM_UNAVAILABLE", True),
        (_http_error(503), "UPSTREAM_UNAVAILABLE", True),
        (_WithStatus(529), "UPSTREAM_UNAVAILABLE", True),
        (_http_error(408), "TIMEOUT", True),
        (_http_error(401), "AUTH", False),
        (_http_error(403), "AUTH", False),
        (_http_error(400), "BAD_REQUEST", False),
        (_http_error(404), "BAD_REQUEST", False),
        # 本地库被占用
        (sqlite3.OperationalError("database is locked"), "DB_BUSY", True),
        (sqlite3.OperationalError("no such table: x"), "INTERNAL", False),
        # 需要人处理的
        (LLMRefusalError("refused"), "REFUSAL", False),
        (LLMNotConfiguredError("no model"), "NOT_CONFIGURED", False),
        (ValueError("LLM 连续输出非法 JSON"), "INVALID_OUTPUT", False),
        (FileNotFoundError(2, "no such file"), "MISSING_PREREQUISITE", False),
        (RuntimeError("bug"), "INTERNAL", False),
        (KeyError("x"), "INTERNAL", False),
        # 前置条件不满足：是 ValueError 的子类，但用它自己的错误码，不落到 INVALID_OUTPUT
        (PreconditionError("IDEA_CONTEXT_EMPTY", "库里没有论文"), "IDEA_CONTEXT_EMPTY", False),
        (_wrapped(PreconditionError("SOME_PRECONDITION", "缺东西")), "SOME_PRECONDITION", False),
        # 外面包了一层普通异常：看里面的起因
        (_wrapped(httpx.ReadTimeout("")), "TIMEOUT", True),
        (_wrapped(_http_error(401)), "AUTH", False),
        (_wrapped(RuntimeError("bug")), "INVALID_OUTPUT", False),
    ],
)
def test_failure_classification(exc, code, transient):
    found = classify_failure(exc)
    assert (found.code, found.transient) == (code, transient)


# ---- 引擎兜底 ----


async def _project(client, email: str) -> tuple[str, dict]:
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "robust"}, headers=headers)
    return resp.json()["id"], headers


async def _demo_voyage(client, email: str) -> tuple[uuid.UUID, dict]:
    project_id, headers = await _project(client, email)
    resp = await client.post(
        "/api/voyages",
        json={"kind": "demo", "project_id": project_id, "goal": "不会悬着"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"]), headers


async def _pipeline_voyage(project_id: str, action: str) -> uuid.UUID:
    """一条只有一步的固定流程任务（pipeline 模式：步骤失败直接停，不重规划）。"""
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            project_id=uuid.UUID(project_id),
            kind="wiki_ingest",
            mode="pipeline",
            goal="t",
            status="planning",
            plan=[{"title": "会出错的一步", "action": action, "params": {}}],
        )
        session.add(run)
        await session.commit()
        return run.id


def _engine(router: LLMRouter | None = None) -> VoyageEngine:
    return VoyageEngine(event_bus=RecordingBus(), llm_router=router or LLMRouter())


async def _load(run_id: uuid.UUID) -> VoyageRun:
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run is not None
        return run


@pytest.mark.parametrize(
    ("raised", "code", "transient"),
    [
        (httpx.ConnectError("connection refused"), "NETWORK", True),
        (ValueError("LLM 连续输出非法 JSON"), "INVALID_OUTPUT", False),
    ],
)
async def test_raising_action_pauses_with_a_stored_reason(
    client, queue_stub, monkeypatch, raised, code, transient
):
    project_id, headers = await _project(client, f"robust-action-{code.lower()}@example.com")

    async def boom(ctx, params):
        raise raised

    monkeypatch.setitem(actions_registry._REGISTRY, "test.boom", boom)
    run_id = await _pipeline_voyage(project_id, "test.boom")

    await _engine().run(run_id)

    run = await _load(run_id)
    assert run.status == "paused_error"
    error = run.checkpoint["last_error"]
    assert (error["code"], error["transient"], error["source"]) == (code, transient, "action")
    assert error["message"] and error["step_title"] == "会出错的一步"
    assert str(raised) in error["detail"]
    # 读接口把它带出来，界面不用碰 checkpoint
    read = VoyageRead.model_validate(run)
    assert read.last_error is not None
    assert (read.last_error.code, read.last_error.transient) == (code, transient)


def _router_failing_at(stage: str, exc: Exception) -> LLMRouter:
    """这个环节的模型调用一律抛 ``exc``（普通补全和结构化输出两个入口都拦）。"""
    router = LLMRouter()

    def failing(original):
        async def call(called_stage, *args, **kwargs):
            if called_stage == stage:
                raise exc
            return await original(called_stage, *args, **kwargs)

        return call

    router.complete = failing(router.complete)  # type: ignore[method-assign]
    router.complete_json = failing(router.complete_json)  # type: ignore[method-assign]
    return router


async def test_precondition_error_keeps_its_code_from_helm_to_last_error(
    client, queue_stub, monkeypatch
):
    """动作抛「前置条件不满足」：错误码一路带到 observation 和任务停下来的原因里。

    以前写成 ``ValueError("IDEA_CONTEXT_EMPTY: …")``，归类时只认得它是个 ValueError，
    记成 INVALID_OUTPUT，真正的码埋在 detail 的文字里。
    """
    raised = PreconditionError("IDEA_CONTEXT_EMPTY", "关联的文献库里没有可用的论文")
    # 原来按 ValueError 接、按子串找错误码的地方照常工作
    assert isinstance(raised, ValueError)
    assert str(raised) == "IDEA_CONTEXT_EMPTY: 关联的文献库里没有可用的论文"
    assert raised.user_message == "关联的文献库里没有可用的论文"

    async def boom(ctx, params):
        raise raised

    monkeypatch.setitem(actions_registry._REGISTRY, "test.precondition", boom)

    observation = await Helm().execute(None, {"action": "test.precondition"})  # type: ignore[arg-type]
    assert "IDEA_CONTEXT_EMPTY" in observation["error"]
    failure = observation["failure"]
    assert (failure["code"], failure["transient"]) == ("IDEA_CONTEXT_EMPTY", False)

    project_id, _headers = await _project(client, "robust-precondition@example.com")
    run_id = await _pipeline_voyage(project_id, "test.precondition")
    await _engine().run(run_id)

    run = await _load(run_id)
    assert run.status == "paused_error"
    error = run.checkpoint["last_error"]
    assert (error["code"], error["transient"], error["source"]) == (
        "IDEA_CONTEXT_EMPTY",
        False,
        "action",
    )
    # 给人看的那句话用异常自己带的说明，而不是查不到错误码时的「程序内部出错」
    assert "关联的文献库里没有可用的论文" in error["message"]
    assert "IDEA_CONTEXT_EMPTY" in error["detail"]
    assert "auto_retry" not in run.checkpoint  # 重试没有用，不自己再试


async def test_verifier_call_that_raises_pauses_instead_of_hanging(client, queue_stub):
    run_id, headers = await _demo_voyage(client, "robust-sextant@example.com")

    # 以前这个超时直接掀翻驱动任务，状态永远停在 verifying
    await _engine(_router_failing_at("sextant", httpx.ReadTimeout(""))).run(run_id)

    detail = (await client.get(f"/api/voyages/{run_id}", headers=headers)).json()
    assert detail["status"] == "paused_error"
    error = detail["last_error"]
    assert (error["code"], error["transient"], error["source"]) == ("TIMEOUT", True, "verifier")
    assert "ReadTimeout" in error["detail"]
    # 半路的步骤不留在「执行中」
    assert "running" not in {s["status"] for s in detail["steps"]}
    messages = (await client.get(f"/api/voyages/{run_id}/messages", headers=headers)).json()
    assert any("任务已暂停" in m["text"] for m in messages)

    # 网络好了，重试能接着跑完；上次的原因不再挂着
    resp = await client.post(f"/api/voyages/{run_id}/resume", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["last_error"] is None
    await _engine().resume(run_id)
    detail = (await client.get(f"/api/voyages/{run_id}", headers=headers)).json()
    assert detail["status"] == "done"
    assert detail["last_error"] is None
    assert "last_error" not in ((await _load(run_id)).checkpoint or {})


async def test_planner_call_that_raises_pauses(client, queue_stub):
    run_id, _headers = await _demo_voyage(client, "robust-planner@example.com")
    engine = _engine()

    async def plan(run, context):
        raise ConnectionResetError(104, "Connection reset by peer")

    engine.navigator.plan = plan  # type: ignore[method-assign]
    await engine.run(run_id)

    run = await _load(run_id)
    assert run.status == "paused_error"
    error = run.checkpoint["last_error"]
    assert (error["code"], error["transient"], error["source"]) == ("NETWORK", True, "planner")

    await _engine().resume(run_id)
    assert (await _load(run_id)).status == "done"


async def test_driver_bug_pauses_as_permanent(client, queue_stub):
    run_id, _headers = await _demo_voyage(client, "robust-driver@example.com")
    engine = _engine()

    async def loop(session, run):
        raise RuntimeError("engine bug")

    engine._loop = loop  # type: ignore[method-assign]
    await engine.run(run_id)

    run = await _load(run_id)
    assert run.status == "paused_error"
    error = run.checkpoint["last_error"]
    assert (error["code"], error["transient"], error["source"]) == ("INTERNAL", False, "driver")


async def test_worker_task_pauses_when_the_engine_cannot_even_start(
    client, queue_stub, monkeypatch
):
    run_id, _headers = await _demo_voyage(client, "robust-task@example.com")

    def broken_engine():
        raise RuntimeError("no engine")

    monkeypatch.setattr(worker_tasks, "_make_engine", broken_engine)
    await worker_tasks.run_voyage({}, str(run_id))

    run = await _load(run_id)
    assert run.status == "paused_error"
    assert run.checkpoint["last_error"]["code"] == "INTERNAL"


async def test_pausing_never_overrides_a_cancel_or_a_pending_question(client, queue_stub):
    run_id, _headers = await _demo_voyage(client, "robust-cancel@example.com")
    for status in ("cancelled", "paused_ask", "done"):
        async with get_sessionmaker()() as session:
            run = await session.get(VoyageRun, run_id)
            run.status = status
            await session.commit()
        assert await park_run(get_sessionmaker(), None, run_id, exc=RuntimeError("x")) is False
        run = await _load(run_id)
        assert run.status == status
        assert "last_error" not in (run.checkpoint or {})


# ---- 对账认领的次数上限 ----


class _RecordingRedis:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, func: str, *args, **kwargs):
        self.jobs.append((func, args, kwargs))


async def _seed_in_flight(project_id: str, *, old: bool = False) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            project_id=uuid.UUID(project_id),
            kind="experiment",
            mode="loop",
            goal="t",
            status="executing",
        )
        if old:
            run.created_at = utcnow() - timedelta(hours=2)
        session.add(run)
        await session.commit()
        return run.id


async def test_reclaims_without_progress_stop_and_pause_the_run(client):
    project_id, _headers = await _project(client, "robust-reclaim@example.com")
    run_id = await _seed_in_flight(project_id)
    redis = _RecordingRedis()

    for _ in range(MAX_RECLAIMS_WITHOUT_PROGRESS):
        await reconcile_stuck_voyages({"redis": redis})
    assert len(redis.jobs) == MAX_RECLAIMS_WITHOUT_PROGRESS == 3
    # 自动恢复不给步骤全新的重试额度
    assert all(kwargs["reset_attempts"] is False for _f, _a, kwargs in redis.jobs)
    assert (await _load(run_id)).status == "executing"

    # 拉了三次一步没动：第四次不再拉，停下来等人
    await reconcile_stuck_voyages({"redis": redis})
    assert len(redis.jobs) == 3
    run = await _load(run_id)
    assert run.status == "paused_error"
    error = run.checkpoint["last_error"]
    assert (error["code"], error["transient"], error["source"]) == (
        "NO_PROGRESS",
        False,
        "reconcile",
    )
    assert "3 次" in error["message"]
    # 停好的任务不再被认领
    await reconcile_stuck_voyages({"redis": redis})
    assert len(redis.jobs) == 3


async def test_periodic_reclaim_shares_the_same_cap(client):
    project_id, _headers = await _project(client, "robust-reclaim-stale@example.com")
    run_id = await _seed_in_flight(project_id, old=True)
    redis = _RecordingRedis()

    # 启动认领与周期回收数的是同一个计数
    await reconcile_stuck_voyages({"redis": redis})
    await reconcile_stale_voyages({"redis": redis}, stale_minutes=30)
    await reconcile_stale_voyages({"redis": redis}, stale_minutes=30)
    assert len(redis.jobs) == 3
    await reconcile_stale_voyages({"redis": redis}, stale_minutes=30)
    assert len(redis.jobs) == 3
    assert (await _load(run_id)).status == "paused_error"


async def test_progress_between_reclaims_resets_the_count(client):
    project_id, _headers = await _project(client, "robust-reclaim-progress@example.com")
    run_id = await _seed_in_flight(project_id)
    redis = _RecordingRedis()

    for round_no in range(6):
        await reconcile_stuck_voyages({"redis": redis})
        # 每两次认领之间任务往前走了一步
        if round_no % 2 == 1:
            async with get_sessionmaker()() as session:
                run = await session.get(VoyageRun, run_id)
                run.cursor = run.cursor + 1
                await session.commit()

    assert len(redis.jobs) == 6
    run = await _load(run_id)
    assert run.status == "executing"
    assert run.checkpoint["reclaim"]["count"] == 2


async def test_a_live_driver_is_not_counted_as_a_reclaim(client):
    project_id, _headers = await _project(client, "robust-reclaim-live@example.com")
    run_id = await _seed_in_flight(project_id)

    class _Busy(_RecordingRedis):
        def is_running(self, job_id: str) -> bool:
            return True

    redis = _Busy()
    for _ in range(5):
        await reconcile_stuck_voyages({"redis": redis})
    run = await _load(run_id)
    assert redis.jobs == [] and run.status == "executing"
    assert "reclaim" not in (run.checkpoint or {})


async def test_reclaim_resume_keeps_step_attempts(client, queue_stub):
    """对账恢复不清零尝试次数；人点的重试才清零。"""
    project_id, _headers = await _project(client, "robust-attempts@example.com")

    calls = 0

    async def flaky(ctx, params):
        nonlocal calls
        calls += 1
        return {"content": "ok"}

    actions_registry._REGISTRY["test.flaky"] = flaky
    try:
        run_id = await _pipeline_voyage(project_id, "test.flaky")
        async with get_sessionmaker()() as session:
            run = await session.get(VoyageRun, run_id)
            run.status = "executing"
            session.add(
                VoyageStep(
                    run_id=run_id,
                    seq=0,
                    rank=0.0,
                    title="会出错的一步",
                    action="test.flaky",
                    params={},
                    status="running",
                    attempt=1,
                )
            )
            await session.commit()

        async def attempts() -> int:
            async with get_sessionmaker()() as session:
                return (
                    await session.execute(
                        select(VoyageStep.attempt).where(VoyageStep.run_id == run_id)
                    )
                ).scalar_one()

        await worker_tasks.resume_voyage({}, str(run_id), reset_attempts=False)
        assert calls == 1
        assert await attempts() == 2
    finally:
        actions_registry._REGISTRY.pop("test.flaky", None)


# ---- Sextant 的判定走结构化输出 ----


async def test_sextant_verdict_failures_are_told_apart(app):
    """给不出合法判定 = 这一步没通过；调用本身失败 = 抛 SextantError 交给引擎停下来。

    判定以前是从回答文字里切 JSON：引号、多一个花括号都会让它解析失败。
    """
    from app.agents.voyage.sextant import Sextant, SextantError, VerdictOut
    from app.core.llm.errors import LLMRateLimitedError, LLMSchemaError
    from app.core.llm.fake import FakeProvider

    provider = FakeProvider()
    router = LLMRouter()
    router.override_provider(provider)
    sextant = Sextant(router)
    run = VoyageRun(id=uuid.uuid4(), kind="demo", goal="t")
    step = {"title": "写一段总结", "action": "llm.complete", "acceptance": "提到了结论"}
    observation = {"content": '总结：实验"成功"了 {见附录}'}

    verdict, usage = await sextant.verify(run, step, observation)
    assert verdict["passed"] is True and verdict["reason"]
    assert usage["prompt_tokens"] > 0

    provider.fail_next(LLMSchemaError("sextant: bad shape"), schema=VerdictOut)
    verdict, _usage = await sextant.verify(run, step, observation)
    assert verdict["passed"] is False and "合法的判定" in verdict["reason"]

    provider.fail_next(LLMRateLimitedError("HTTP 429"), schema=VerdictOut)
    with pytest.raises(SextantError) as caught:
        await sextant.verify(run, step, observation)
    found = classify_failure(caught.value)
    assert (found.code, found.transient) == ("LLM_RATE_LIMITED", True)
