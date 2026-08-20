"""ask 原语（paused_ask）：agent 死路转向用户提问，回答后推进。

契约：
- on_failure=fail 步骤失败：有人值守 → paused_ask（提问带候选项）；
  无人值守（created_by 空，cron 发起）→ 降级 paused_error（没人会来回答）；
- 提问幂等（reconcile 重放不重复建）；回答后 resume：retry 复位节点续跑、
  abort 置 failed（唯一由用户决定的失败路径）；
- 双答 409；paused_ask 下 resume 端点 409（回答即恢复，不给绕过门）；
- cancel 把 open 提问置 superseded；
- 完成标准未达成 / 预算耗尽 / 重规划超限 也转提问；
- paused_ask 不在 IN_FLIGHT_STATUSES（worker reconcile 绝不能绕过提问自动续跑）。
"""

import uuid

from sqlalchemy import select

from app.agents.voyage.engine import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.user import User
from app.models.voyage import IN_FLIGHT_STATUSES, VoyageMessage, VoyageRun, VoyageStep
from app.services import voyage_messages as messages_service
from tests.conftest import RecordingBus, register_and_login

FATAL_PLAN = [
    {
        "title": "关键步骤",
        "action": "sleep",
        "params": {"seconds": -1},
        "acceptance": None,
        "requires_gate": None,
        "on_failure": "fail",
        "budget": {"max_attempts": 1},
    }
]

_OK_STEP = {
    "title": "普通步骤",
    "action": "sleep",
    "params": {"seconds": 0},
    "acceptance": None,
    "checks": [{"kind": "no_error"}],
    "requires_gate": None,
}


async def _make_project(client) -> tuple[str, dict]:
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "ask-proj"}, headers=headers)
    return resp.json()["id"], headers


async def _run(
    project_id: str,
    *,
    kind: str = "custom",
    plan: list[dict],
    attended: bool = True,
    budget: dict | None = None,
    usage: dict | None = None,
    checkpoint: dict | None = None,
    done_criteria: dict | None = None,
) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        created_by = None
        if attended:
            created_by = (
                await session.execute(select(User.id).order_by(User.created_at))
            ).scalars().first()
        run = VoyageRun(
            kind=kind,
            goal="ask 契约测试",
            status="planning",
            cursor=0,
            plan=plan,
            budget=budget,
            usage=usage,
            checkpoint=checkpoint,
            done_criteria=done_criteria,
            project_id=uuid.UUID(project_id),
            created_by=created_by,
        )
        session.add(run)
        await session.commit()
        return run.id


def _engine() -> tuple[VoyageEngine, RecordingBus]:
    bus = RecordingBus()
    return VoyageEngine(event_bus=bus, llm_router=LLMRouter()), bus


async def _open_ask_of(run_id: uuid.UUID) -> VoyageMessage | None:
    async with get_sessionmaker()() as session:
        return await messages_service.open_ask(session, run_id)


def test_paused_ask_not_in_flight():
    """reconcile 认领集绝不能包含 paused_ask：等人不等 worker。"""
    assert "paused_ask" not in IN_FLIGHT_STATUSES


async def test_fatal_step_attended_asks(client, queue_stub):
    project_id, headers = await _make_project(client)
    run_id = await _run(project_id, plan=FATAL_PLAN)
    engine, bus = _engine()
    await engine.run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "paused_ask"
        step = (
            await session.execute(select(VoyageStep).where(VoyageStep.run_id == run_id))
        ).scalar_one()
        assert step.status == "failed"  # 失败留痕，不复位不重跑
    ask = await _open_ask_of(run_id)
    assert ask is not None and (ask.payload or {}).get("ask_kind") == "fatal_step"
    assert (ask.payload or {}).get("options"), "提问应带候选项"
    # SSE ask.created + WS voyage.ask 都发了
    assert any(e == "ask.created" for _v, e, _d in bus.voyage_events)
    assert any(m.get("type") == "voyage.ask" for _p, m in bus.notify)
    # 详情带 open_ask
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    assert resp.json()["open_ask"]["id"] == str(ask.id)


async def test_fatal_step_unattended_degrades_to_paused_error(client, queue_stub):
    project_id, _headers = await _make_project(client)
    run_id = await _run(project_id, plan=FATAL_PLAN, attended=False)
    engine, _ = _engine()
    await engine.run(run_id)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "paused_error"
    assert await _open_ask_of(run_id) is None


async def test_raise_ask_idempotent_on_replay(client, queue_stub):
    """重复驱动（reconcile 重放场景）不重复建提问。"""
    project_id, _headers = await _make_project(client)
    run_id = await _run(project_id, plan=FATAL_PLAN)
    engine, _ = _engine()
    await engine.run(run_id)
    await engine.run(run_id)  # 重放
    async with get_sessionmaker()() as session:
        asks = (
            await session.execute(
                select(VoyageMessage).where(
                    VoyageMessage.run_id == run_id, VoyageMessage.kind == "ask"
                )
            )
        ).scalars().all()
        assert len(asks) == 1 and asks[0].status == "open"


async def test_answer_retry_resumes_and_consumes(client, queue_stub, bus_recorder):
    project_id, headers = await _make_project(client)
    run_id = await _run(project_id, plan=FATAL_PLAN)
    engine, _ = _engine()
    await engine.run(run_id)
    ask = await _open_ask_of(run_id)
    assert ask is not None

    # "修好"参数后回答：重试
    async with get_sessionmaker()() as session:
        step = (
            await session.execute(select(VoyageStep).where(VoyageStep.run_id == run_id))
        ).scalar_one()
        step.params = {"seconds": 0}
        await session.commit()
    resp = await client.post(
        f"/api/voyages/{run_id}/asks/{ask.id}/answer",
        json={"choice": "retry", "text": "参数已修，重试"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["kind"] == "answer" and resp.json()["reply_to"] == str(ask.id)
    # 状态推进 + 入队续跑
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "executing"
    assert ("resume_voyage", (str(run_id),), {}) in queue_stub.jobs

    await engine.resume(run_id)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"
        messages = (
            await session.execute(
                select(VoyageMessage)
                .where(VoyageMessage.run_id == run_id)
                .order_by(VoyageMessage.seq)
            )
        ).scalars().all()
        the_ask = next(m for m in messages if m.kind == "ask")
        assert the_ask.status == "consumed"
        # 回答文本镜像成建议消息，并在重试执行时被消费进 params
        mirror = next(m for m in messages if m.kind == "chat")
        assert mirror.consumed_at is not None
        step = (
            await session.execute(
                select(VoyageStep).where(
                    VoyageStep.run_id == run_id, VoyageStep.status == "passed"
                )
            )
        ).scalar_one()
        assert "参数已修，重试" in (step.params.get("user_guidance") or [])


async def test_answer_abort_fails_voyage(client, queue_stub, bus_recorder):
    project_id, headers = await _make_project(client)
    run_id = await _run(project_id, plan=FATAL_PLAN)
    engine, _ = _engine()
    await engine.run(run_id)
    ask = await _open_ask_of(run_id)

    resp = await client.post(
        f"/api/voyages/{run_id}/asks/{ask.id}/answer",
        json={"choice": "abort"},
        headers=headers,
    )
    assert resp.status_code == 201
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "failed"  # 人拍板的失败：真终态
        the_ask = await session.get(VoyageMessage, ask.id)
        assert the_ask.status == "consumed"
    # abort 不入队续跑
    assert not any(job[0] == "resume_voyage" for job in queue_stub.jobs)


async def test_double_answer_409(client, queue_stub, bus_recorder):
    project_id, headers = await _make_project(client)
    run_id = await _run(project_id, plan=FATAL_PLAN)
    engine, _ = _engine()
    await engine.run(run_id)
    ask = await _open_ask_of(run_id)

    first = await client.post(
        f"/api/voyages/{run_id}/asks/{ask.id}/answer",
        json={"choice": "retry", "text": "先试"},
        headers=headers,
    )
    assert first.status_code == 201
    second = await client.post(
        f"/api/voyages/{run_id}/asks/{ask.id}/answer",
        json={"choice": "abort"},
        headers=headers,
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "ASK_NOT_OPEN"


async def test_resume_endpoint_blocked_while_asking(client, queue_stub):
    project_id, headers = await _make_project(client)
    run_id = await _run(project_id, plan=FATAL_PLAN)
    engine, _ = _engine()
    await engine.run(run_id)
    resp = await client.post(f"/api/voyages/{run_id}/resume", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "VOYAGE_WAITING_ANSWER"


async def test_cancel_supersedes_open_ask(client, queue_stub):
    project_id, headers = await _make_project(client)
    run_id = await _run(project_id, plan=FATAL_PLAN)
    engine, _ = _engine()
    await engine.run(run_id)
    resp = await client.post(f"/api/voyages/{run_id}/cancel", headers=headers)
    assert resp.status_code == 200
    async with get_sessionmaker()() as session:
        asks = (
            await session.execute(
                select(VoyageMessage).where(
                    VoyageMessage.run_id == run_id, VoyageMessage.kind == "ask"
                )
            )
        ).scalars().all()
        assert [a.status for a in asks] == ["superseded"]
    # 已取消：回答入口自然 409（open 提问已不存在）
    resp = await client.post(
        f"/api/voyages/{run_id}/asks/{asks[0].id}/answer",
        json={"choice": "retry"},
        headers=headers,
    )
    assert resp.status_code == 409


async def test_cancel_supersedes_an_in_flight_stop_claim(client, queue_stub):
    project_id, headers = await _make_project(client)
    run_id = await _run(project_id, plan=FATAL_PLAN)
    engine, _ = _engine()
    await engine.run(run_id)
    ask = await _open_ask_of(run_id)
    assert ask is not None

    async with get_sessionmaker()() as session:
        claimed = await messages_service.claim_open_ask_for_stop(session, ask.id)
        assert claimed is True

    resp = await client.post(f"/api/voyages/{run_id}/cancel", headers=headers)
    assert resp.status_code == 200
    async with get_sessionmaker()() as session:
        stopped_ask = await session.get(VoyageMessage, ask.id)
        assert stopped_ask.status == "superseded"


async def test_done_criteria_unmet_asks_then_accept(client, queue_stub, bus_recorder):
    """完成标准未达成 → 提问；用户接受为完成 → done（不再一律 paused_error）。"""
    project_id, headers = await _make_project(client)
    run_id = await _run(
        project_id,
        plan=[_OK_STEP],
        done_criteria={"checks": [{"kind": "artifact_exists", "key": "artifacts.missing"}]},
    )
    engine, _ = _engine()
    await engine.run(run_id)
    ask = await _open_ask_of(run_id)
    assert ask is not None and (ask.payload or {}).get("ask_kind") == "done_criteria"

    resp = await client.post(
        f"/api/voyages/{run_id}/asks/{ask.id}/answer",
        json={"choice": "accept"},
        headers=headers,
    )
    assert resp.status_code == 201
    await engine.resume(run_id)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"


async def test_budget_exhausted_asks_then_add_budget(client, queue_stub, bus_recorder):
    """预算耗尽且无收尾可救 → 提问；追加预算后续跑到完成。"""
    project_id, headers = await _make_project(client)
    run_id = await _run(
        project_id,
        plan=[_OK_STEP],
        budget={"max_tokens": 1},
        usage={"total_tokens": 999_999},
    )
    engine, _ = _engine()
    await engine.run(run_id)
    ask = await _open_ask_of(run_id)
    assert ask is not None and (ask.payload or {}).get("ask_kind") == "budget"

    resp = await client.post(
        f"/api/voyages/{run_id}/asks/{ask.id}/answer",
        json={"choice": "add_budget", "payload": {"add_tokens": 2_000_000}},
        headers=headers,
    )
    assert resp.status_code == 201
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.budget["max_tokens"] == 2_000_001  # 原 1 + 追加 200 万
    await engine.resume(run_id)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"


async def test_action_initiated_ask_pauses_without_burning_attempt(
    client, queue_stub, bus_recorder
):
    """动作返回 observation["ask"]（如 analyze 不确定时）：不进验证、不消耗尝试预算，
    节点回 pending；回答后重跑该步，回答文本经建议消息注入 params。"""
    project_id, headers = await _make_project(client)
    run_id = await _run(project_id, plan=[_OK_STEP])
    engine, _ = _engine()

    asked = False
    orig_execute = engine.helm.execute

    async def ask_once(ctx, step_def):
        nonlocal asked
        if not asked:
            asked = True
            return {"ask": {"question": "结果反常，继续用当前方案吗？", "ask_kind": "action_ask"}}
        return await orig_execute(ctx, step_def)

    engine.helm.execute = ask_once
    await engine.run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "paused_ask"
        step = (
            await session.execute(select(VoyageStep).where(VoyageStep.run_id == run_id))
        ).scalar_one()
        assert step.status == "pending" and step.attempt == 0  # 提问不消耗尝试预算
        assert len(step.attempts or []) == 1  # 但留痕
    ask = await _open_ask_of(run_id)
    assert ask is not None and (ask.payload or {}).get("ask_kind") == "action_ask"

    resp = await client.post(
        f"/api/voyages/{run_id}/asks/{ask.id}/answer",
        json={"choice": "continue", "text": "继续，注意记录中间结果"},
        headers=headers,
    )
    assert resp.status_code == 201
    await engine.resume(run_id)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"
        step = (
            await session.execute(select(VoyageStep).where(VoyageStep.run_id == run_id))
        ).scalar_one()
        assert "继续，注意记录中间结果" in (step.params.get("user_guidance") or [])


async def test_done_criteria_continue_appends_even_with_bad_anchor(
    client, queue_stub, bus_recorder
):
    """完成标准未达成 → 用户「继续补做」：LLM 给的扩展哪怕 insert_after 指向
    已完成节点 / 混入 update 操作，也被归一化为末尾追加——不再被应用期不变量
    拒绝、把用户逼进「补充指示 → 再被拒」的死循环（线上实测致死原因）。"""
    project_id, headers = await _make_project(client)
    run_id = await _run(
        project_id,
        plan=[_OK_STEP],
        done_criteria={"checks": [{"kind": "artifact_exists", "key": "artifacts.missing"}]},
    )
    engine, _ = _engine()
    await engine.run(run_id)
    ask = await _open_ask_of(run_id)
    assert ask is not None and (ask.payload or {}).get("ask_kind") == "done_criteria"

    async with get_sessionmaker()() as session:
        first_step_id = (
            await session.execute(select(VoyageStep.id).where(VoyageStep.run_id == run_id))
        ).scalars().first()

    async def bad_anchor_edit(run, failed_step, diagnosis, plan_state, user_guidance=None):
        return {
            "finish": False,
            "reason": "按用户指示补一步",
            "edits": [
                {  # update 已完成节点：应被归一化丢弃
                    "op": "update_node",
                    "step_id": str(first_step_id),
                    "patch": {"title": "不该生效"},
                },
                {  # insert_after 指向已完成节点：应被强制改为末尾追加；
                    # requires_gate 应被剥掉（用户刚拍板补做，不该再连环审批——
                    # 线上实测 LLM 还会塞动作名当闸门种类）
                    "op": "add_nodes",
                    "insert_after": str(first_step_id),
                    "nodes": [
                        {
                            "title": "补做一步",
                            "action": "sleep",
                            "params": {"seconds": 0},
                            "acceptance": "已完成",
                            "checks": [{"kind": "no_error"}],
                            "requires_gate": "experiment.run",
                        }
                    ],
                }
            ],
        }

    engine.navigator.on_result = bad_anchor_edit

    resp = await client.post(
        f"/api/voyages/{run_id}/asks/{ask.id}/answer",
        json={"choice": "continue", "text": "补一步收集指标"},
        headers=headers,
    )
    assert resp.status_code == 201
    await engine.resume(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        steps = (
            (
                await session.execute(
                    select(VoyageStep).where(VoyageStep.run_id == run_id).order_by(VoyageStep.rank)
                )
            )
            .scalars()
            .all()
        )
        # 扩展生效：新步骤追加在末尾并执行通过；没有陷入 replan_error 提问
        assert len(steps) == 2
        assert steps[-1].title == "补做一步" and steps[-1].status == "passed"
        assert steps[0].title != "不该生效"
        # 完成标准依旧未达成 → 再次转提问（而非报编辑被拒）
        assert run.status == "paused_ask"
    ask2 = await _open_ask_of(run_id)
    assert (ask2.payload or {}).get("ask_kind") == "done_criteria"


async def test_replan_limit_asks_then_guidance_continues(client, queue_stub, bus_recorder):
    """计划调整超限 → 提问；用户给指示后计数清零、按指示继续到完成。"""
    project_id, headers = await _make_project(client)
    plan = [
        {
            "title": "机械校验不过的步骤",
            "action": "sleep",
            "params": {"seconds": 0},
            "acceptance": None,
            "checks": [{"kind": "exit_code", "value": 0}],  # sleep 无 exit_code → 判断类失败
            "requires_gate": None,
        }
    ]
    run_id = await _run(project_id, plan=plan, checkpoint={"replans": 2})
    engine, _ = _engine()
    await engine.run(run_id)
    ask = await _open_ask_of(run_id)
    assert ask is not None and (ask.payload or {}).get("ask_kind") == "no_progress"

    resp = await client.post(
        f"/api/voyages/{run_id}/asks/{ask.id}/answer",
        json={"choice": "continue", "text": "换一个能通过的步骤"},
        headers=headers,
    )
    assert resp.status_code == 201
    await engine.resume(run_id)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"  # fake navigator 按指示给出替代计划后跑完
        assert int((run.checkpoint or {}).get("replans", 0)) == 1  # 清零后新一轮调整
