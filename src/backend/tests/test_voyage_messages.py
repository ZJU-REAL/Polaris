"""任务对话流（voyage_messages）：API 收发 + 引擎在决策点消费用户建议。

- POST/GET /voyages/{id}/messages：追加建议（终态 409）与按 seq 回放；
- 注入点①：步骤执行前把未消费建议注入 step.params["user_guidance"]，
  消费标记与 running 同一事务（resume 重放不重复注入）；
- 注入点②：loop 失败回灌时未消费建议交给 Navigator（user_guidance 形参）。
"""

import uuid

import pytest
from sqlalchemy import select

from app.agents.voyage.engine import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.voyage import VoyageMessage, VoyageRun, VoyageStep
from app.services import voyage_messages as messages_service
from tests.conftest import RecordingBus, register_and_login


async def _make_project(client) -> tuple[str, dict]:
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "msg-proj"}, headers=headers)
    return resp.json()["id"], headers


async def _manual_run(project_id: str, *, kind: str, plan: list[dict], status="planning"):
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind=kind,
            goal="对话流测试",
            status=status,
            cursor=0,
            plan=plan,
            project_id=uuid.UUID(project_id),
        )
        session.add(run)
        await session.commit()
        return run.id


def _engine() -> VoyageEngine:
    return VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())


_SLEEP_OK = {
    "title": "确定性步骤",
    "action": "sleep",
    "params": {"seconds": 0},
    "acceptance": None,
    "checks": [{"kind": "no_error"}],
    "requires_gate": None,
}


# ---- API ----


async def test_post_and_list_messages(client, bus_recorder):
    project_id, headers = await _make_project(client)
    run_id = await _manual_run(project_id, kind="custom", plan=[_SLEEP_OK])

    resp = await client.post(
        f"/api/voyages/{run_id}/messages", json={"text": "先跑小样本"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["seq"] == 1 and body["role"] == "user" and body["kind"] == "chat"
    assert body["consumed_at"] is None

    resp = await client.post(
        f"/api/voyages/{run_id}/messages", json={"text": "预算别超"}, headers=headers
    )
    assert resp.json()["seq"] == 2

    # 发布了 SSE message 事件（前端对话流实时追加）
    events = [e for (vid, e, _d) in bus_recorder.voyage_events if e == "message"]
    assert len(events) == 2

    resp = await client.get(f"/api/voyages/{run_id}/messages", headers=headers)
    assert resp.status_code == 200
    seqs = [m["seq"] for m in resp.json()]
    assert seqs == [1, 2]

    # after_seq 增量拉取
    resp = await client.get(f"/api/voyages/{run_id}/messages?after_seq=1", headers=headers)
    assert [m["seq"] for m in resp.json()] == [2]


async def test_post_message_terminal_voyage_409(client, bus_recorder):
    project_id, headers = await _make_project(client)
    run_id = await _manual_run(project_id, kind="custom", plan=[_SLEEP_OK], status="done")
    resp = await client.post(
        f"/api/voyages/{run_id}/messages", json={"text": "太晚了"}, headers=headers
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "VOYAGE_ALREADY_FINISHED"


async def test_messages_require_view_permission(client, bus_recorder):
    project_id, _headers = await _make_project(client)
    run_id = await _manual_run(project_id, kind="custom", plan=[_SLEEP_OK])
    outsider = await register_and_login(client, email="mallory@example.com")
    outsider_headers = {"Authorization": f"Bearer {outsider}"}
    resp = await client.get(f"/api/voyages/{run_id}/messages", headers=outsider_headers)
    assert resp.status_code == 404
    resp = await client.post(
        f"/api/voyages/{run_id}/messages", json={"text": "hi"}, headers=outsider_headers
    )
    assert resp.status_code == 404


# ---- 引擎消费 ----


async def test_step_execution_drains_guidance_into_params(client, queue_stub):
    """注入点①：执行前未消费建议进 step.params["user_guidance"] 并标记消费。"""
    project_id, _headers = await _make_project(client)
    run_id = await _manual_run(project_id, kind="custom", plan=[_SLEEP_OK])
    async with get_sessionmaker()() as session:
        await messages_service.append_message(
            session, run_id, role="user", kind="chat", text="注意用小学习率"
        )

    await _engine().run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"
        step = (
            await session.execute(select(VoyageStep).where(VoyageStep.run_id == run_id))
        ).scalar_one()
        assert step.params["user_guidance"] == ["注意用小学习率"]
        messages = (
            (
                await session.execute(
                    select(VoyageMessage)
                    .where(VoyageMessage.run_id == run_id)
                    .order_by(VoyageMessage.seq)
                )
            )
            .scalars()
            .all()
        )
        chat = [m for m in messages if m.kind == "chat"]
        assert len(chat) == 1
        assert chat[0].consumed_at is not None
        assert chat[0].consumed_step_id == step.id
        # 消费后有 agent 播报（用户能看到建议被采纳）
        infos = [m for m in messages if m.kind == "info" and m.role == "agent"]
        assert len(infos) == 1 and "1 条建议" in infos[0].text


async def test_navigator_edit_receives_pending_guidance(client, queue_stub):
    """注入点②：失败回灌时执行期间新到的建议交给 Navigator（user_guidance）。"""
    project_id, _headers = await _make_project(client)
    plan = [
        {
            "title": "会失败的步骤",
            "action": "sleep",
            "params": {"seconds": -1},
            "acceptance": None,
            "requires_gate": None,
        }
    ]
    run_id = await _manual_run(project_id, kind="custom", plan=plan)

    engine = _engine()
    exec_count = 0
    orig_execute = engine.helm.execute

    async def execute_and_post(ctx, step_def):
        nonlocal exec_count
        exec_count += 1
        observation = await orig_execute(ctx, step_def)
        # 模拟用户在步骤执行期间发来建议
        async with get_sessionmaker()() as session:
            await messages_service.append_message(
                session, run_id, role="user", kind="chat", text=f"建议 {exec_count}"
            )
        return observation

    engine.helm.execute = execute_and_post

    captured: dict = {}
    orig_on_result = engine.navigator.on_result

    async def spy_on_result(run, failed_step, diagnosis, plan_state, user_guidance=None):
        captured["guidance"] = user_guidance
        return await orig_on_result(
            run, failed_step, diagnosis, plan_state, user_guidance=user_guidance
        )

    engine.navigator.on_result = spy_on_result
    await engine.run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"  # fake navigator 给出替代计划后跑完
        rows = (
            (
                await session.execute(
                    select(VoyageStep).where(VoyageStep.run_id == run_id).order_by(VoyageStep.seq)
                )
            )
            .scalars()
            .all()
        )
        failed = rows[0]
        # 第 1 条建议在原地重试（第 2 次执行）时注入了失败节点 params
        assert failed.params["user_guidance"] == ["建议 1"]
        # 第 2 条建议在计划调整时交给了 Navigator
        assert captured["guidance"] is not None and "建议 2" in captured["guidance"]
        # 最后一次执行期间发的建议之后再无决策点，留在队列（其余全部被消费）
        pending = await messages_service.pending_chat_messages(session, run_id)
        assert [m.text for m in pending] == [f"建议 {exec_count}"]


async def test_append_message_seq_is_monotonic(client):
    project_id, _headers = await _make_project(client)
    run_id = await _manual_run(project_id, kind="custom", plan=[_SLEEP_OK])
    async with get_sessionmaker()() as session:
        first = await messages_service.append_message(
            session, run_id, role="user", kind="chat", text="a"
        )
        second = await messages_service.append_message(
            session, run_id, role="agent", kind="info", text="b"
        )
        assert (first.seq, second.seq) == (1, 2)
        rows = await messages_service.list_messages(session, run_id)
        assert [m.text for m in rows] == ["a", "b"]


@pytest.mark.parametrize("bad", ["", "x" * 8001])
async def test_post_message_validation(client, bus_recorder, bad):
    project_id, headers = await _make_project(client)
    run_id = await _manual_run(project_id, kind="custom", plan=[_SLEEP_OK])
    resp = await client.post(f"/api/voyages/{run_id}/messages", json={"text": bad}, headers=headers)
    assert resp.status_code == 422
