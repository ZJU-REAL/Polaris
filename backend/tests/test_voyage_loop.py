"""任务循环 v1（docs/voyage-loop.md 阶段 A-E）：检查注册表 + 引擎失败分派 + 计划编辑。

- checks：确定性检查注册表的判定与 actionable 诊断；
- pipeline 失败 → paused_error（不经 LLM 重规划），resume 复位失败节点后续跑；
- 判断类失败（校验未过）不原地重试；执行类错误在 max_attempts 内带诊断重试；
- 每次尝试归档进 step.attempts；重规划旧节点标 obsolete 留痕不删行；
- PlanEdit 操作集 schema 校验 + experiment plan_signal 确定性分支表的幂等。
"""

import uuid

import pytest
from sqlalchemy import select

from app.agents.voyage.checks import run_deterministic_checks, validate_checks
from app.agents.voyage.engine import VoyageEngine
from app.agents.voyage.navigator import validate_steps
from app.agents.voyage.plan_edit import experiment_signal_edits, validate_plan_edit
from app.core.db import get_sessionmaker
from app.core.llm.router import LLMRouter
from app.models.voyage import VoyageRun, VoyageStep
from tests.conftest import RecordingBus, register_and_login

# ---- checks 注册表 ----


def test_checks_no_error_and_exit_code():
    verdict, rubrics = run_deterministic_checks(
        [{"kind": "no_error"}], observation={"content": "ok"}, checkpoint={}
    )
    assert verdict["passed"] is True and rubrics == []

    verdict, _ = run_deterministic_checks(
        [{"kind": "no_error"}], observation={"error": "boom"}, checkpoint={}
    )
    assert verdict["passed"] is False and "boom" in verdict["reason"]

    verdict, _ = run_deterministic_checks(
        [{"kind": "exit_code", "value": 0}], observation={"exit_code": 2}, checkpoint={}
    )
    assert verdict["passed"] is False
    assert "exit_code 2" in verdict["reason"] and "期望 0" in verdict["reason"]


def test_checks_metric_min_count_artifact_schema():
    obs = {"metrics": {"accuracy": 0.9}, "papers": [1, 2, 3], "plan": {"primary_metric": "acc"}}
    cp = {"artifacts": {"report.md": "# 内容"}}
    checks = [
        {"kind": "metric", "name": "accuracy", "op": ">=", "value": 0.8},
        {"kind": "min_count", "field": "papers", "value": 2},
        {"kind": "artifact_exists", "key": "artifacts.report.md"},
        {"kind": "schema_valid", "field": "plan", "required_keys": ["primary_metric"]},
    ]
    verdict, rubrics = run_deterministic_checks(checks, observation=obs, checkpoint=cp)
    assert verdict["passed"] is True and rubrics == []

    verdict, _ = run_deterministic_checks(
        [{"kind": "metric", "name": "accuracy", "op": ">=", "value": 0.95}],
        observation=obs,
        checkpoint=cp,
    )
    assert verdict["passed"] is False and "0.9" in verdict["reason"]

    verdict, _ = run_deterministic_checks(
        [{"kind": "min_count", "field": "papers", "value": 5}], observation=obs, checkpoint=cp
    )
    assert verdict["passed"] is False and "3 < 最低要求 5" in verdict["reason"]


def test_checks_llm_rubric_deferred_and_validate():
    verdict, rubrics = run_deterministic_checks(
        [{"kind": "no_error"}, {"kind": "llm_rubric", "rubric": "覆盖关键问题"}],
        observation={"content": "ok"},
        checkpoint={},
    )
    assert verdict is None and len(rubrics) == 1  # 确定性全过 → rubric 交回 LLM 判定

    with pytest.raises(ValueError):
        validate_checks([{"kind": "unknown_kind"}])
    with pytest.raises(ValueError):
        validate_checks("not-a-list")


# ---- PlanEdit 操作集（阶段 D/E）----


def test_validate_plan_edit_schema():
    ok = validate_plan_edit(
        {
            "reason": "补一个替代步骤",
            "edits": [
                {
                    "op": "add_nodes",
                    "insert_after": None,
                    "nodes": [
                        {
                            "title": "替代步骤",
                            "action": "sleep",
                            "params": {"seconds": 0},
                            "acceptance": "已等待完成",
                        }
                    ],
                }
            ],
        },
        step_validator=validate_steps,
    )
    assert ok["finish"] is False and len(ok["edits"]) == 1
    assert ok["edits"][0]["nodes"][0]["acceptance"] == "已等待完成"

    fin = validate_plan_edit(
        {"finish": True, "reason": "按当前结果收束"}, step_validator=validate_steps
    )
    assert fin["finish"] is True and fin["edits"] == []

    for bad in (
        "not-an-object",
        {"edits": [{"op": "teleport"}]},  # 未知操作
        {"edits": []},  # 空编辑（应改用 finish）
        # 新节点缺验收（sleep 属内容动作，不补缺省 checks）
        {
            "edits": [
                {
                    "op": "add_nodes",
                    "nodes": [{"title": "t", "action": "sleep", "params": {}}],
                }
            ]
        },
        {"edits": [{"op": "update_node", "step_id": "x"}]},  # 无补丁字段
        {"edits": [{"op": "obsolete_nodes", "step_ids": []}]},  # 空作废列表
    ):
        with pytest.raises(ValueError):
            validate_plan_edit(bad, step_validator=validate_steps)


class _RowStub:
    def __init__(self, action: str, status: str) -> None:
        self.action = action
        self.status = status


def test_experiment_node_failure_semantics():
    """experiment mode=loop 的节点级失败语义（docs/voyage-loop.md §7）：
    run/smoke 硬停（on_failure=fail + max_attempts=1，防盲目重跑烧算力/重复修复循环），
    plan/setup/analyze/figures/report 走 loop 回灌（原地重试 → AI 计划调整）。"""
    from app.agents.voyage.navigator import experiment_plan
    from app.agents.voyage.plan_edit import experiment_wrapup_nodes
    from app.models.voyage import mode_for_kind

    assert mode_for_kind("experiment") == "loop"

    nodes = {n["action"]: n for n in experiment_plan(None)}  # run 参数未用
    assert nodes["experiment.smoke"]["on_failure"] == "fail"
    assert nodes["experiment.smoke"]["budget"] == {"max_attempts": 1}
    assert nodes["experiment.run"]["on_failure"] == "fail"
    assert nodes["experiment.run"]["budget"] == {"max_attempts": 1}
    for action in ("experiment.plan", "experiment.setup", "experiment.analyze"):
        assert "on_failure" not in nodes[action], action
    for n in experiment_wrapup_nodes():
        assert "on_failure" not in n, n["action"]


def test_experiment_signal_edits_idempotent():
    """分支表幂等：待办节点已存在则不重复追加（防 resume 重放）。"""
    rows = [_RowStub("experiment.analyze", "passed")]
    edit = experiment_signal_edits({"decision": "continue", "next_round": 2}, rows)
    assert edit is not None
    actions = [n["action"] for n in edit["edits"][0]["nodes"]]
    assert actions == ["experiment.run", "experiment.analyze"]

    rows.append(_RowStub("experiment.run", "pending"))
    assert experiment_signal_edits({"decision": "continue", "next_round": 2}, rows) is None

    edit = experiment_signal_edits({"decision": "finish", "stopped_reason": "no_improve"}, rows)
    assert [n["action"] for n in edit["edits"][0]["nodes"]] == [
        "experiment.figures",
        "experiment.report",
    ]
    rows.append(_RowStub("experiment.report", "pending"))
    assert experiment_signal_edits({"decision": "finish"}, rows) is None
    assert experiment_signal_edits({"decision": "unknown"}, rows) is None


# ---- 引擎失败分派 ----


async def _make_project(client) -> tuple[str, dict]:
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "loop-proj"}, headers=headers)
    return resp.json()["id"], headers


async def _manual_run(
    project_id: str,
    *,
    kind: str,
    plan: list[dict],
    budget: dict | None = None,
    usage: dict | None = None,
) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind=kind,
            goal="任务循环测试",
            status="planning",
            cursor=0,
            plan=plan,
            budget=budget,
            usage=usage,
            project_id=uuid.UUID(project_id),
        )
        session.add(run)
        await session.commit()
        return run.id


def _engine() -> VoyageEngine:
    return VoyageEngine(event_bus=RecordingBus(), llm_router=LLMRouter())


async def test_pipeline_failure_pauses_then_resume_retries(client, queue_stub):
    """pipeline 执行类错误：默认不隐式重试 → paused_error；resume 复位节点续跑。"""
    project_id, _headers = await _make_project(client)
    plan = [
        {
            "title": "会失败的确定性步骤",
            "action": "sleep",
            "params": {"seconds": -1},
            "acceptance": None,
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
        }
    ]
    # wiki_bootstrap ∈ PIPELINE_KINDS：mode 在首次驱动时对齐为 pipeline
    run_id = await _manual_run(project_id, kind="wiki_bootstrap", plan=plan)

    engine = _engine()
    await engine.run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.mode == "pipeline"
        assert run.status == "paused_error"  # 不 LLM 重规划、不 failed，等人工修复
        assert "replans" not in (run.checkpoint or {})
        step = (
            await session.execute(select(VoyageStep).where(VoyageStep.run_id == run_id))
        ).scalar_one()
        assert step.status == "failed"
        assert step.attempt == 1  # pipeline 默认 max_attempts=1：无隐式重试
        assert len(step.attempts) == 1  # 尝试归档落库
        assert step.attempts[0]["verdict"]["passed"] is False
        # "修复代码"：把参数改对，模拟修复后断点重试
        step.params = {"seconds": 0}
        await session.commit()

    await engine.resume(run_id)
    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"
        step = (
            await session.execute(select(VoyageStep).where(VoyageStep.run_id == run_id))
        ).scalar_one()
        assert step.status == "passed"
        assert len(step.attempts) == 2  # 失败 + 修复后成功各归档一次


async def test_judgment_failure_no_inplace_retry(client, queue_stub):
    """判断类失败（校验未过、无执行错误）不原地重试，pipeline 直接暂停。"""
    project_id, _headers = await _make_project(client)
    plan = [
        {
            "title": "机械校验不过的步骤",
            "action": "sleep",
            "params": {"seconds": 0},
            "acceptance": None,
            # sleep 的 observation 无 exit_code → 判断类失败
            "checks": [{"kind": "exit_code", "value": 0}],
            "requires_gate": None,
        }
    ]
    run_id = await _manual_run(project_id, kind="wiki_ingest", plan=plan)
    await _engine().run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "paused_error"
        step = (
            await session.execute(select(VoyageStep).where(VoyageStep.run_id == run_id))
        ).scalar_one()
        assert step.attempt == 1 and len(step.attempts) == 1
        assert "exit_code" in step.verdict["reason"]  # actionable 诊断：指明哪条检查


async def test_loop_execution_error_retries_then_replans(client, queue_stub):
    """loop 模式执行类错误：先带诊断原地重试，重试尽后计划编辑；旧节点 obsolete 留痕。"""
    project_id, headers = await _make_project(client)
    plan = [
        {
            "title": "会失败的步骤",
            "action": "sleep",
            "params": {"seconds": -1},
            "acceptance": None,
            "requires_gate": None,
        }
    ]
    run_id = await _manual_run(project_id, kind="custom", plan=plan)  # 未知 kind → loop
    await _engine().run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.mode == "loop"
        assert run.status == "done"  # fake navigator 重规划出替代计划后跑完
        assert run.checkpoint["replans"] == 1
        assert run.plan_iteration == 1
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
        assert failed.status == "obsolete"  # 留痕不删行
        assert failed.attempt == 2  # loop 默认 max_attempts=2：重试过一次
        assert len(failed.attempts) == 2
        assert failed.params.get("diagnosis")  # 重试带诊断
        assert all(r.status == "passed" for r in rows[1:])
        assert all(r.seq > failed.seq for r in rows[1:])  # seq 只增不改

    # 详情 API：默认只回活动清单，include_obsolete=true 才含作废节点（任务板开关）
    resp = await client.get(f"/api/voyages/{run_id}", headers=headers)
    detail = resp.json()
    assert detail["plan_iteration"] == 1 and detail["mode"] == "loop"
    assert all(s["status"] != "obsolete" for s in detail["steps"])
    # 计划调整历史落库并随详情返回（因果叙事：谁触发、为什么、加了几步）
    assert len(detail["plan_history"]) == 1
    event = detail["plan_history"][0]
    assert event["source"] == "navigator" and event["iteration"] == 1
    assert event["trigger_step"] == "会失败的步骤"
    assert event["reason"] and event["added"] >= 1 and event["obsoleted"] >= 1
    # 步骤携带验收与溯源（任务板展示"怎样算通过"与"第几次调整创建"）
    step = detail["steps"][0]
    assert "acceptance" in step and "provenance" in step and "attempts" in step
    resp = await client.get(f"/api/voyages/{run_id}?include_obsolete=true", headers=headers)
    steps = resp.json()["steps"]
    assert any(s["status"] == "obsolete" for s in steps)
    assert all("rank" in s and "attempt" in s for s in steps)


async def test_budget_exhausted_runs_wrapup_step(client, queue_stub):
    """预算耗尽降级收尾（docs/voyage-loop.md §5.4）：昂贵步骤已完成、预算超限时，
    廉价收尾步骤（wrapup）仍放行把结果落地，未执行的非收尾步骤作废——不再一刀切
    paused_error 白费已完成的工作（idea_review 汇总被预算门挡死的真实场景）。"""
    project_id, headers = await _make_project(client)
    plan = [
        {
            "title": "昂贵步骤（已完成）",
            "action": "sleep",
            "params": {"seconds": 0},
            "acceptance": None,
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
        },
        {
            "title": "汇总收尾",
            "action": "sleep",
            "params": {"seconds": 0},
            "acceptance": None,
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
            "wrapup": True,
        },
    ]
    # 预算 1 token、已用 999999：驱动一开始就超限
    run_id = await _manual_run(
        project_id,
        kind="idea_review",
        plan=plan,
        budget={"max_tokens": 1},
        usage={"total_tokens": 999_999},
    )
    await _engine().run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "done"  # 不是 paused_error
        rows = (
            (
                await session.execute(
                    select(VoyageStep).where(VoyageStep.run_id == run_id).order_by(VoyageStep.seq)
                )
            )
            .scalars()
            .all()
        )
        assert rows[0].status == "obsolete"  # 非收尾步骤被作废（预算已耗尽不再执行）
        assert rows[1].status == "passed"  # 收尾步骤放行跑完
        history = (run.checkpoint or {}).get("plan_history") or []
        assert any(e["source"] == "budget" and e["obsoleted"] == 1 for e in history)


async def test_budget_exhausted_no_wrapup_pauses(client, queue_stub):
    """预算耗尽且无收尾步骤可救：仍 paused_error（等人工加预算）。"""
    project_id, headers = await _make_project(client)
    plan = [
        {
            "title": "普通步骤",
            "action": "sleep",
            "params": {"seconds": 0},
            "acceptance": None,
            "checks": [{"kind": "no_error"}],
            "requires_gate": None,
        }
    ]
    run_id = await _manual_run(
        project_id,
        kind="idea_review",
        plan=plan,
        budget={"max_tokens": 1},
        usage={"total_tokens": 999_999},
    )
    await _engine().run(run_id)

    async with get_sessionmaker()() as session:
        run = await session.get(VoyageRun, run_id)
        assert run.status == "paused_error"
