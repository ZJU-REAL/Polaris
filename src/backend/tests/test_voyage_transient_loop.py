"""loop / template 模式里的临时性故障：停下来按 1/5/20 分钟自己再试，不找 Navigator 改计划。

线上实测（voyage 8ee97715）：共享模型网关回 503「No available accounts」，寻找假设的
第一步「生成根假设」失败。loop 模式把失败交给 Navigator（它本身也要调模型）：先插了
一个 sleep 60 秒的步骤再重跑，又失败，Navigator 判「没有进展」转成问用户「按当前结果
收尾，还是给出指示继续？」——一轮都没跑，研究就显示「等你回复」。对话流里那条播报
还带着整段 503 原始报错。

临时性故障换个计划也没用，跟 pipeline 模式一样停到 paused_error 等自动重试；只有
需要人处理的失败才交给 Navigator。
"""

import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.agents.voyage import actions as actions_registry
from app.agents.voyage.actions_discovery import SeedOut
from app.agents.voyage.engine import VoyageEngine
from app.agents.voyage.failures import AUTO_RETRY_DELAYS_MINUTES
from app.core.db import get_sessionmaker
from app.core.llm.errors import LLMOverloadedError, LLMRequestError
from app.core.llm.fake import FakeProvider
from app.core.llm.router import LLMRouter
from app.models.base import utcnow
from app.models.voyage import VoyageMessage, VoyageRun, VoyageStep
from app.services import research_runs as research
from tests.conftest import RecordingBus
from tests.test_voyage_discovery import _make_workspace
from worker.tasks import retry_paused_voyages

pytestmark = pytest.mark.usefixtures("small_library_ok")

# 线上那次 503 的原始报错（节选）：只能出现在 detail 里，不能出现在给人看的话里
RAW_503 = (
    "HTTP 503 api_error: Error code: 503 - {'type': 'error', 'error': {'type': 'api_error', "
    "'message': 'litellm.ServiceUnavailableError: AnthropicException - "
    '{"error":{"message":"No available accounts: no available accounts"}}\'}}'
)
BUSY_NOTICE = "模型服务暂时繁忙，约 {m} 分钟后自动重试（第 {n} 次，共 3 次）。"


class _Gateway(FakeProvider):
    """共享模型网关的替身：``down`` 时生成根假设那一次结构化调用回 503，其余照常。"""

    def __init__(self, error: Exception | None = None) -> None:
        super().__init__()
        self.down = True
        self.error = error
        self.seed_calls = 0

    async def complete_structured(self, messages, *, model, schema, **kwargs):
        if schema is SeedOut:
            self.seed_calls += 1
            if self.down:
                raise self.error or LLMOverloadedError(RAW_503)
        return await super().complete_structured(messages, model=model, schema=schema, **kwargs)


class _Router(LLMRouter):
    """记下每一次模型调用的 stage；``navigator_error`` 让 Navigator 自己的调用失败。"""

    def __init__(self, provider: FakeProvider) -> None:
        super().__init__()
        self.override_provider(provider)
        self.stages: list[str] = []
        self.navigator_error: Exception | None = None

    async def complete(self, stage, *args, **kwargs):
        self.stages.append(stage)
        if stage == "navigator" and self.navigator_error is not None:
            raise self.navigator_error
        return await super().complete(stage, *args, **kwargs)

    async def complete_json(self, stage, *args, **kwargs):
        self.stages.append(stage)
        return await super().complete_json(stage, *args, **kwargs)


class _RecordingRedis:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, func: str, *args, **kwargs):
        self.jobs.append((func, args, kwargs))


async def _discovery(client) -> uuid.UUID:
    """经创建接口建一条有人值守的寻找假设任务（有发起人：死路会转成问用户）。"""
    project_id, library_id, headers = await _make_workspace(client)
    resp = await client.post(
        "/api/voyages",
        json={
            "kind": "discovery",
            "project_id": project_id,
            "goal": "可验证的 agent 工作流",
            "params": {
                "direction": "可验证的 agent 工作流",
                "library_id": str(library_id),
                "max_expansions": 1,
            },
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return uuid.UUID(resp.json()["id"])


async def _load(run_id: uuid.UUID) -> VoyageRun:
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run is not None
        return run


async def _steps(run_id: uuid.UUID) -> list[VoyageStep]:
    async with get_sessionmaker()() as session:
        stmt = select(VoyageStep).where(VoyageStep.run_id == run_id).order_by(VoyageStep.seq)
        return list((await session.execute(stmt)).scalars().all())


async def _messages(run_id: uuid.UUID) -> list[VoyageMessage]:
    async with get_sessionmaker()() as session:
        stmt = (
            select(VoyageMessage).where(VoyageMessage.run_id == run_id).order_by(VoyageMessage.seq)
        )
        return list((await session.execute(stmt)).scalars().all())


def _due(run: VoyageRun) -> datetime:
    return datetime.fromisoformat(run.checkpoint["auto_retry"]["next_at"])


async def _retry_when_due(run_id: uuid.UUID, router: _Router) -> None:
    """定时任务到点把它拉起来，再跑一遍队列里那个 resume。"""
    redis = _RecordingRedis()
    due = _due(await _load(run_id))
    assert await retry_paused_voyages({"redis": redis}, now=due) == [str(run_id)]
    assert redis.jobs[-1][:2] == ("resume_voyage", (str(run_id),))
    await VoyageEngine(event_bus=RecordingBus(), llm_router=router).resume(run_id)


def _assert_plain(text: str) -> None:
    for raw in ("503", "No available accounts", "Error code", "litellm", "{"):
        assert raw not in text, text


async def test_busy_model_at_the_first_step_waits_and_retries_instead_of_asking(client, queue_stub):
    run_id = await _discovery(client)
    gateway = _Gateway()
    router = _Router(gateway)

    started = utcnow()
    await VoyageEngine(event_bus=RecordingBus(), llm_router=router).run(run_id)

    run = await _load(run_id)
    assert run.status == "paused_error"
    assert run.checkpoint["auto_retry"]["attempt"] == 1
    due = _due(run)
    assert timedelta(seconds=55) < due - started < timedelta(seconds=65)
    error = run.checkpoint["last_error"]
    assert (error["code"], error["transient"], error["source"]) == (
        "LLM_OVERLOADED",
        True,
        "action",
    )
    _assert_plain(error["message"])
    assert "No available accounts" in error["detail"]
    # 没有去找 Navigator：没有计划调整、没有插 sleep、没有重排
    assert "navigator" not in router.stages
    assert "plan_history" not in run.checkpoint and "replans" not in run.checkpoint
    # 原地重试一次（和以前一样），还是 503 就停下等自动重试
    assert gateway.seed_calls == 2
    steps = await _steps(run_id)
    assert [(s.action, s.status) for s in steps] == [("hypothesis.seed", "failed")]
    # 对话流：没有提问；一句话说清楚什么时候再试，原始报错只在 payload 里
    messages = await _messages(run_id)
    assert [m.kind for m in messages if m.kind == "ask"] == []
    infos = [m for m in messages if m.kind == "info"]
    assert [m.text for m in infos] == [BUSY_NOTICE.format(m=1, n=1)]
    assert "No available accounts" in infos[0].payload["detail"]
    assert infos[0].step_id == steps[0].id

    # 一分钟后网关恢复：同一步重跑，任务一路跑完
    gateway.down = False
    await _retry_when_due(run_id, router)
    run = await _load(run_id)
    assert run.status == "done", run.checkpoint.get("last_error")
    assert "auto_retry" not in run.checkpoint and "last_error" not in run.checkpoint
    assert gateway.seed_calls == 3
    assert "navigator" not in router.stages
    steps = await _steps(run_id)
    assert steps[0].action == "hypothesis.seed" and steps[0].status == "passed"
    assert all(s.status == "passed" for s in steps)
    assert "sleep" not in {s.action for s in steps}


async def test_busy_model_through_every_retry_stays_paused_and_needs_attention(client, queue_stub):
    run_id = await _discovery(client)
    gateway = _Gateway()
    router = _Router(gateway)
    await VoyageEngine(event_bus=RecordingBus(), llm_router=router).run(run_id)

    for attempt in range(1, len(AUTO_RETRY_DELAYS_MINUTES) + 1):
        run = await _load(run_id)
        assert run.status == "paused_error"
        assert run.checkpoint["auto_retry"]["attempt"] == attempt
        await _retry_when_due(run_id, router)

    run = await _load(run_id)
    assert run.status == "paused_error"
    assert run.checkpoint["auto_retry"] == {"attempt": 3, "next_at": None}
    assert run.checkpoint["last_error"]["transient"] is True
    # 第一次 + 三次自动重试，每次都带一次原地重试；尝试次数没有越用越少
    assert gateway.seed_calls == 8
    assert "navigator" not in router.stages
    messages = await _messages(run_id)
    assert [m for m in messages if m.kind == "ask"] == []
    infos = [m.text for m in messages if m.kind == "info"]
    assert infos == [
        BUSY_NOTICE.format(m=1, n=1),
        BUSY_NOTICE.format(m=5, n=2),
        BUSY_NOTICE.format(m=20, n=3),
        "模型服务暂时繁忙，已自动重试 3 次仍未恢复，任务已暂停，可以稍后手动重试。",
    ]
    for text in infos:
        _assert_plain(text)

    # 研究那一行：寻找假设这一步需要处理，原因是模型服务连不上，给「重试」
    async with get_sessionmaker()() as session:
        child = await research._child_facts(session, run)  # noqa: SLF001
    assert child is not None and child.failed_action == "hypothesis.seed"
    decision = research.decide("hypotheses", "running", research.Facts(child=child))
    assert decision.status == "needs_attention"
    assert decision.attention["code"] == "LLM_UNREACHABLE"
    assert decision.attention["action"] == "retry"


async def test_a_failure_that_needs_a_person_still_goes_to_the_navigator(client, queue_stub):
    run_id = await _discovery(client)
    gateway = _Gateway(LLMRequestError("HTTP 400 invalid_request_error: prompt is too long"))
    router = _Router(gateway)
    await VoyageEngine(event_bus=RecordingBus(), llm_router=router).run(run_id)

    run = await _load(run_id)
    # 原地带诊断重试一次，然后交给 Navigator 改计划（替身的编辑用了不允许的动作，
    # 被拒后转成问用户）——和以前一样，不排自动重试
    assert gateway.seed_calls == 2
    assert "navigator" in router.stages
    assert run.status == "paused_ask"
    assert "auto_retry" not in run.checkpoint


async def test_busy_model_during_replanning_waits_and_retries(client, queue_stub):
    """Navigator 自己的模型调用撞上 503：同样停下来等自动重试，不报失败也不问人。"""
    run_id = await _discovery(client)
    gateway = _Gateway(LLMRequestError("HTTP 400 invalid_request_error: prompt is too long"))
    router = _Router(gateway)
    router.navigator_error = LLMOverloadedError(RAW_503)
    await VoyageEngine(event_bus=RecordingBus(), llm_router=router).run(run_id)

    run = await _load(run_id)
    assert run.status == "paused_error"
    assert run.checkpoint["auto_retry"]["attempt"] == 1
    error = run.checkpoint["last_error"]
    assert (error["code"], error["transient"], error["source"]) == (
        "LLM_OVERLOADED",
        True,
        "planner",
    )
    _assert_plain(error["message"])
    messages = await _messages(run_id)
    assert [m for m in messages if m.kind == "ask"] == []
    infos = [m for m in messages if m.kind == "info"]
    assert [m.text for m in infos] == [BUSY_NOTICE.format(m=1, n=1)]
    assert "No available accounts" in infos[0].payload["detail"]


class _Flaky:
    def __init__(self, exc: Exception) -> None:
        self.exc: Exception | None = exc
        self.calls = 0

    async def __call__(self, ctx, params):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return {"content": "ok"}


@pytest.mark.parametrize(("kind", "mode"), [("demo", "loop"), ("idea_proposal", "template")])
async def test_transient_step_failure_is_not_replanned_in_any_mode(
    client, queue_stub, monkeypatch, kind, mode
):
    from tests.conftest import register_and_login

    token = await register_and_login(client, f"transient-{mode}@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id = (await client.post("/api/projects", json={"name": "t"}, headers=headers)).json()[
        "id"
    ]
    action = _Flaky(httpx.ConnectError("connection refused"))
    monkeypatch.setitem(actions_registry._REGISTRY, "test.flaky", action)  # noqa: SLF001
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            project_id=uuid.UUID(project_id),
            kind=kind,
            mode=mode,
            goal="t",
            status="planning",
            plan=[{"title": "会断网的一步", "action": "test.flaky", "params": {}}],
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    engine = VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())

    async def _no_navigator(*_args, **_kwargs):
        raise AssertionError("transient failures must not be sent to the navigator")

    monkeypatch.setattr(engine.navigator, "on_result", _no_navigator)
    monkeypatch.setattr(engine.navigator, "replan", _no_navigator)
    await engine.run(run_id)

    run = await _load(run_id)
    assert run.mode == mode
    assert run.status == "paused_error"
    assert run.checkpoint["auto_retry"]["attempt"] == 1
    assert run.checkpoint["last_error"]["code"] == "NETWORK"
    assert action.calls == 2  # 原地重试一次，然后停下等自动重试
    infos = [m.text for m in await _messages(run_id) if m.kind == "info"]
    assert infos == ["网络连接失败，约 1 分钟后自动重试（第 1 次，共 3 次）。"]
