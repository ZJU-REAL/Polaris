"""Voyage 引擎闭环测试：不起真 worker，直接 await engine.run/resume；
LLM 走 fake provider（无 DB 路由时的默认回退），事件总线用记录器。"""

import uuid

from sqlalchemy import select

from app.agents.voyage.engine import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.llm_config import LLMUsage
from app.models.voyage import VoyageRun
from tests.conftest import RecordingBus, register_and_login


async def _setup_demo_voyage(client, goal="研究 LLM 推理加速的关键路径"):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "voyage-proj"}, headers=headers)
    project_id = resp.json()["id"]
    resp = await client.post(
        "/api/voyages",
        json={"kind": "demo", "project_id": project_id, "goal": goal},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json(), project_id, headers


def _make_engine() -> tuple[VoyageEngine, RecordingBus]:
    bus = RecordingBus()
    return VoyageEngine(event_bus=bus, llm_router=LLMRouter()), bus


async def test_demo_voyage_full_loop(client, queue_stub, bus_recorder):
    voyage, project_id, headers = await _setup_demo_voyage(client)
    run_id = voyage["id"]
    assert voyage["status"] == "planning"
    assert queue_stub.jobs == [("run_voyage", (run_id,), {})]

    engine, bus = _make_engine()
    await engine.run(uuid.UUID(run_id))

    # 第 2 步（seq=1）声明 compute_budget 闸门 → 暂停
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "paused_gate"
    assert detail["cursor"] == 1
    assert len(detail["plan"]) == 3
    steps = detail["steps"]
    assert steps[0]["status"] == "passed"
    assert steps[0]["verdict"]["passed"] is True
    assert steps[0]["observation"]["content"].startswith("[fake:")
    assert steps[1]["status"] == "pending"

    # 引擎发布过 status / step / log 事件
    kinds = {e[1] for e in bus.voyage_events}
    assert {"status", "step", "log"} <= kinds
    statuses = [e[2]["status"] for e in bus.voyage_events if e[1] == "status"]
    assert "paused_gate" in statuses
    # gate.created 广播到项目通知频道
    assert any(m["type"] == "gate.created" for _, m in bus.notify)

    # 闸门落库且 payload 关联 voyage
    resp = await client.get(f"/api/gates?project_id={project_id}", headers=headers)
    gates = resp.json()
    assert len(gates) == 1
    gate = gates[0]
    assert gate["kind"] == "compute_budget"
    assert gate["payload"]["voyage_id"] == run_id
    assert gate["payload"]["step_seq"] == 1

    # 审批通过 → 入队 resume_voyage
    resp = await client.post(
        f"/api/gates/{gate['id']}/approve", json={"comment": "预算 OK"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    assert ("resume_voyage", (run_id,), {}) in queue_stub.jobs

    # 恢复 → 跑完剩余步骤到 done
    await engine.resume(uuid.UUID(run_id))
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "done"
    assert detail["cursor"] == 3
    steps = detail["steps"]
    assert [s["status"] for s in steps] == ["passed", "passed", "passed"]
    assert all(s["verdict"]["passed"] for s in steps)

    # checkpoint：artifact.write 的产物落入 checkpoint；usage 累加
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, uuid.UUID(run_id))
        assert run.checkpoint["artifacts"]["demo-report.md"].startswith("# Demo 任务产物")
        assert run.usage["total_tokens"] > 0
        usage_rows = (
            (await session.execute(select(LLMUsage).where(LLMUsage.voyage_id == run.id)))
            .scalars()
            .all()
        )
        assert usage_rows  # llm.complete + sextant 均记账
        assert {r.stage for r in usage_rows} <= {"navigator", "sextant", "default"}


async def test_gate_reject_fails_voyage(client, queue_stub, bus_recorder):
    voyage, project_id, headers = await _setup_demo_voyage(client)
    run_id = voyage["id"]
    engine, _bus = _make_engine()
    await engine.run(uuid.UUID(run_id))

    resp = await client.get(f"/api/gates?project_id={project_id}", headers=headers)
    gate = resp.json()[0]
    resp = await client.post(
        f"/api/gates/{gate['id']}/reject", json={"comment": "预算不够"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"
    assert resp.json()["comment"] == "预算不够"

    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    assert resp.json()["status"] == "failed"
    # 驳回不入队恢复
    assert not any(j[0] == "resume_voyage" for j in queue_stub.jobs)

    # 对已失败航程 resume 是 no-op
    await engine.resume(uuid.UUID(run_id))
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    assert resp.json()["status"] == "failed"


async def test_replan_after_failed_step(client, queue_stub):
    """失败步骤触发重规划：fake navigator 产出替代计划后继续到 done。"""
    _voyage, project_id, headers = await _setup_demo_voyage(client)
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind="custom",
            goal="测试重规划",
            status="planning",
            cursor=0,
            plan=[
                {
                    "title": "会失败的步骤",
                    "action": "sleep",
                    "params": {"seconds": -1},  # 负数 → helm 捕获 ValueError
                    "acceptance": None,
                    "requires_gate": None,
                }
            ],
            project_id=uuid.UUID(project_id),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    engine, bus = _make_engine()
    await engine.run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"
        assert run.checkpoint["replans"] == 1
        assert len(run.checkpoint["replaced_steps"]) == 1
        assert run.checkpoint["replaced_steps"][0]["verdict"]["passed"] is False
    statuses = [e[2]["status"] for e in bus.voyage_events if e[1] == "status"]
    assert "replanning" in statuses


async def test_cancelled_voyage_engine_noop(client, queue_stub):
    voyage, _project_id, headers = await _setup_demo_voyage(client)
    run_id = voyage["id"]
    resp = await client.post(f"/api/voyages/{run_id}/cancel", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    engine, bus = _make_engine()
    await engine.run(uuid.UUID(run_id))  # 终态直接返回

    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "cancelled"
    assert all(s["status"] == "pending" for s in detail["steps"]) or detail["steps"] == []
