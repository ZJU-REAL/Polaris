"""Experiment Lab 全流程测试：fake LLM + MockSSH 全离线，直接驱动 VoyageEngine。

覆盖：plan→gate→approve→setup→smoke（失败修复重试）→run/analyze 轮次（3 轮 improve/stop，
plan_signal 动态追加节点）→
figures→report→done、预算超时 kill、协作式 cancel（轮询循环内）、cancel API、
闸门驳回联动、白名单越界拒绝、创建校验（idea promoted / 凭据归属）与成员权限。
迭代/图表的专项用例见 test_experiment_iterate.py（docs/api-m5-a.md §6）。
"""

import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents.voyage import VoyageEngine
from app.agents.voyage import actions_experiment as ax
from app.core.db import get_sessionmaker
from app.core.llm.base import CompletionResult
from app.core.llm.fake import FakeProvider
from app.core.llm.router import LLMRouter
from app.models.activity import Activity
from app.models.experiment import Experiment, ExperimentRun
from app.models.idea import Idea
from app.models.voyage import VoyageRun
from app.services import ssh_exec
from tests.conftest import RecordingBus, register_and_login
from tests.fake_ssh import FakeSSHConnector, FakeSSHServer, FakeSSHSession
from tests.test_ssh_credentials import PAYLOAD as CRED_PAYLOAD

RUN_LOG = (
    'POLARIS_METRIC {"name": "accuracy", "step": 0, "value": 0.6}\n'
    'POLARIS_METRIC {"name": "accuracy", "step": 1, "value": 0.7}\n'
    'POLARIS_METRIC {"name": "loss", "step": 1, "value": 0.4}\n'
    "not a metric line\n"
    "done (fake experiment)\n"
)


def metric_log(value: float, step: int = 1) -> str:
    return (
        f'POLARIS_METRIC {{"name": "accuracy", "step": {step}, "value": {value}}}\n'
        "done (fake experiment)\n"
    )


FAKE_PNG = b"\x89PNG\r\n\x1a\n(fake png bytes)"


@pytest_asyncio.fixture
async def fake_ssh(app):
    server = FakeSSHServer(
        run_log=RUN_LOG,
        plot_outputs={
            "figures/primary_metric.png": FAKE_PNG,
            "figures/primary_metric.pdf": b"%PDF-1.4 (fake pdf)",
        },
    )
    ssh_exec.set_connector_factory(lambda: FakeSSHConnector(server))
    yield server
    ssh_exec.set_connector_factory(None)


@pytest_asyncio.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(ax, "RUN_POLL_SECONDS", 0)


async def _setup_project(client, email="alice@example.com"):
    token = await register_and_login(client, email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "exp-proj"}, headers=headers)
    assert resp.status_code == 201
    return resp.json()["id"], headers


async def _seed_idea(project_id: str, status: str = "promoted") -> str:
    async with get_sessionmaker()() as session:
        idea = Idea(
            project_id=uuid.UUID(project_id),
            title="共引图增强检索（test idea）",
            summary="用 2-hop 共引特征改进检索",
            content="## 方法概述\n\n……",
            status=status,
        )
        session.add(idea)
        await session.commit()
        return str(idea.id)


async def _create_credential(client, headers) -> str:
    resp = await client.post("/api/ssh-credentials", json=CRED_PAYLOAD, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _create_experiment(client, headers, project_id, idea_id, cred_id, budget=None):
    payload = {"idea_id": idea_id, "credential_id": cred_id}
    if budget is not None:
        payload["params"] = {"budget": budget}
    resp = await client.post(
        f"/api/projects/{project_id}/experiments", json=payload, headers=headers
    )
    return resp


def _make_engine(router: LLMRouter | None = None) -> tuple[VoyageEngine, RecordingBus]:
    bus = RecordingBus()
    return VoyageEngine(event_bus=bus, llm_router=router or LLMRouter()), bus


async def _approve_gate(client, headers, project_id, expected_exp_id=None):
    resp = await client.get(f"/api/gates?project_id={project_id}", headers=headers)
    gates = resp.json()
    assert len(gates) == 1, gates
    gate = gates[0]
    assert gate["kind"] == "compute_budget"
    if expected_exp_id is not None:
        assert gate["payload"]["experiment_id"] == expected_exp_id
    resp = await client.post(f"/api/gates/{gate['id']}/approve", json={}, headers=headers)
    assert resp.status_code == 200
    return gate


async def test_experiment_full_pipeline(client, queue_stub, fake_ssh, bus_recorder):
    # 3 轮迭代路径（improve→improve→stop）：主指标逐轮提升
    fake_ssh.run_logs = [RUN_LOG, metric_log(0.75), metric_log(0.8)]
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)

    resp = await _create_experiment(
        client, headers, project_id, idea_id, cred_id, budget={"max_hours": 2, "max_runs": 3}
    )
    assert resp.status_code == 201, resp.text
    exp = resp.json()
    exp_id = exp["id"]
    assert exp["status"] == "planning"
    assert exp["project_id"] == project_id
    assert exp["idea_title"] == "共引图增强检索（test idea）"
    assert exp["budget"] == {"max_hours": 2, "max_runs": 3, "no_improve_stop": 2}
    assert exp["workdir"] == f"~/polaris_runs/{exp_id}"
    assert exp["server_host"] == CRED_PAYLOAD["host"]
    voyage_id = exp["voyage_id"]
    assert ("run_voyage", (voyage_id,), {}) in queue_stub.jobs

    # 列表可见
    resp = await client.get(f"/api/projects/{project_id}/experiments", headers=headers)
    assert [e["id"] for e in resp.json()] == [exp_id]

    # 阶段一：plan → compute_budget 闸门暂停
    engine, bus = _make_engine()
    await engine.run(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    assert resp.json()["status"] == "paused_gate"
    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "awaiting_gate"
    plan = detail["plan"]
    assert plan["hypotheses"] and all(h["status"] == "testing" for h in plan["hypotheses"])
    assert plan["repro_strategy"]
    assert plan["steps"]
    assert plan["primary_metric"] == {"name": "accuracy", "direction": "maximize"}
    assert plan["budget_estimate"]["gpu_hours"] == 2

    # 闸门 payload 含实验 id 与预算摘要
    gate = await _approve_gate(client, headers, project_id, expected_exp_id=exp_id)
    assert gate["payload"]["budget"] == {"max_hours": 2, "max_runs": 3, "no_improve_stop": 2}
    assert gate["payload"]["plan_summary"]["hypotheses"]
    assert gate["payload"]["voyage_id"] == voyage_id
    assert ("resume_voyage", (voyage_id,), {}) in queue_stub.jobs

    # 阶段二：setup → smoke → 轮次（run+analyze 由 plan_signal 动态追加）→ figures → report
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    voyage = resp.json()
    assert voyage["status"] == "done", voyage
    assert voyage["mode"] == "loop"  # 实验按动态循环走（docs/voyage-loop.md §3 档2）
    # 3 轮 improve→improve→stop：每轮是可见的 run+analyze 节点（docs/voyage-loop.md §7）
    assert [s["status"] for s in voyage["steps"]] == ["passed"] * 11
    assert [s["action"] for s in voyage["steps"]] == [
        "experiment.plan",
        "experiment.setup",
        "experiment.smoke",
        "experiment.run",
        "experiment.analyze",
        "experiment.run",
        "experiment.analyze",
        "experiment.run",
        "experiment.analyze",
        "experiment.figures",
        "experiment.report",
    ]
    last_analyze = voyage["steps"][8]
    assert last_analyze["observation"]["rounds"] == 3
    assert last_analyze["observation"]["stopped_reason"] == "假设已全部有结论（fake）"
    assert last_analyze["observation"]["plan_signal"]["decision"] == "finish"

    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "done"
    # 迭代运行记录：3 轮，seq 递增，主指标逐轮提升
    assert len(detail["runs"]) == 3
    assert [r["seq"] for r in detail["runs"]] == [1, 2, 3]
    assert all(r["status"] == "succeeded" and r["exit_code"] == 0 for r in detail["runs"])
    assert [r["primary_value"] for r in detail["runs"]] == [0.7, 0.75, 0.8]
    run = detail["runs"][0]
    assert run["started_at"] and run["finished_at"]
    assert "run.sh" in run["command"]
    assert "pid" not in run  # 内部字段不出 API
    # reflection 逐轮落库：前两轮 improve，末轮 stop
    assert [r["reflection"]["decision"] for r in detail["runs"]] == ["improve", "improve", "stop"]
    assert detail["runs"][0]["reflection"]["planned_change"]
    # 假设回写：末轮 verified / falsified + evidence
    hyps = detail["plan"]["hypotheses"]
    assert [h["status"] for h in hyps] == ["verified", "falsified"]
    assert all(h["evidence"] for h in hyps)
    # iteration_state 落库
    assert detail["iteration_state"] == {
        "no_improve_streak": 0,
        "debug_count": 0,
        "stopped_reason": "假设已全部有结论（fake）",
    }
    # POLARIS_METRIC 解析进 run 与 experiment
    assert run["metrics"]["accuracy"] == [
        {"step": 0, "value": 0.6},
        {"step": 1, "value": 0.7},
    ]
    assert detail["metrics"]["loss"] == [{"step": 1, "value": 0.4}]
    assert [p["value"] for p in detail["metrics"]["accuracy"]] == [0.6, 0.7, 0.75, 0.8]
    # 图表：LLM 脚本产图 → 拉回本地 → VLM 质检图注 → figures 落库（path 不出 API）
    assert detail["figures"] == [
        {"index": 0, "name": "primary_metric.png", "caption": "（fake）实验图注 0"}
    ]
    resp = await client.get(f"/api/experiments/{exp_id}/figures/0/image", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == FAKE_PNG
    resp = await client.get(f"/api/experiments/{exp_id}/figures/9/image", headers=headers)
    assert resp.status_code == 404
    # 报告
    assert detail["report"].startswith("## 实验报告")

    # 远端产物：代码文件经 SFTP 写入 workdir（含必需文件），venv/smoke/launch/plot 命令有序发生
    sftp_root = f"polaris_runs/{exp_id}"
    for name in ("requirements.txt", "run.sh", "train.py", "metrics_all.json", "plot_figures.py"):
        assert f"{sftp_root}/{name}" in fake_ssh.files
    assert "--smoke" in fake_ssh.files[f"{sftp_root}/run.sh"]
    assert "POLARIS_METRIC" in fake_ssh.files[f"{sftp_root}/train.py"]
    # 平台写的 metrics_all.json 覆盖全部 run；绘图脚本只读该文件
    metrics_all = json.loads(fake_ssh.files[f"{sftp_root}/metrics_all.json"])
    assert [r["seq"] for r in metrics_all["runs"]] == [1, 2, 3]
    assert metrics_all["primary_metric"] == {"name": "accuracy", "direction": "maximize"}
    assert "metrics_all.json" in fake_ssh.files[f"{sftp_root}/plot_figures.py"]
    joined = "\n".join(fake_ssh.commands)
    assert f"mkdir -p ~/polaris_runs/{exp_id}" in joined
    assert "pip install -r requirements.txt" in joined
    assert "bash run.sh --smoke" in joined
    assert joined.count("run.sh > run.log") == 3  # 3 轮 run launch（setup 也 nohup，按 run 标记数）
    assert ".venv/bin/python plot_figures.py" in joined

    # 审计：每条远程命令都有 Activity(kind=ssh.exec)
    async with get_sessionmaker()() as session:
        audits = (
            (
                await session.execute(
                    select(Activity).where(
                        Activity.project_id == uuid.UUID(project_id),
                        Activity.kind == "ssh.exec",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert audits
        assert all(a.payload["host"] == CRED_PAYLOAD["host"] for a in audits)
        audited_cmds = [a.payload["command"] for a in audits]
        assert any("mkdir -p" in c for c in audited_cmds)
        assert any(c.startswith("sftp:write") for c in audited_cmds)

    # WS 状态联动全程有序
    exp_statuses = [m["status"] for _, m in bus.notify if m.get("type") == "experiment.status"]
    assert exp_statuses == ["awaiting_gate", "setup", "running", "reporting", "done"]

    # 本地日志镜像 → logs API
    resp = await client.get(f"/api/experiments/{exp_id}/logs", headers=headers)
    logs = resp.json()
    assert logs["truncated"] is False
    assert any("POLARIS_METRIC" in line for line in logs["lines"])
    assert "done (fake experiment)" in logs["lines"]
    resp = await client.get(
        f"/api/experiments/{exp_id}/logs?run_id={run['id']}&tail=2", headers=headers
    )
    assert resp.json()["truncated"] is True
    assert len(resp.json()["lines"]) == 2

    # SSE：终态实验 → status + log + end 后收流
    async with client.stream(
        "GET", f"/api/experiments/{exp_id}/logs/stream", headers=headers
    ) as resp:
        assert resp.status_code == 200
        body = ""
        async for chunk in resp.aiter_text():
            body += chunk
    assert "event: status" in body
    assert "event: log" in body
    assert body.rstrip().endswith('data: {"status": "done"}')
    assert "event: end" in body


async def test_smoke_failure_fixed_and_retried(client, queue_stub, fake_ssh, bus_recorder):
    """冒烟第一次失败 → stderr 回 LLM 修文件 → 重试通过。"""
    fake_ssh.smoke_exits = [1, 0]
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    voyage = resp.json()
    assert voyage["status"] == "done", voyage
    smoke_step = voyage["steps"][2]
    assert smoke_step["action"] == "experiment.smoke"
    assert smoke_step["observation"] == {"exit_code": 0, "attempts": 2, "fixes": 1}
    assert "\n".join(fake_ssh.commands).count("bash run.sh --smoke") == 2

    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    assert resp.json()["status"] == "done"


async def test_smoke_fixes_unbounded_until_success(client, queue_stub, fake_ssh, bus_recorder):
    """冒烟修复不再按次数设限：连续失败 3 次（超过旧上限）仍继续修，第 4 次通过，
    全程不提问、不打断，整条流水线走完。"""
    fake_ssh.smoke_exits = [1, 1, 1, 0]
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    engine, bus = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    voyage = resp.json()
    assert voyage["status"] == "done", voyage
    smoke_step = next(s for s in voyage["steps"] if s["action"] == "experiment.smoke")
    assert smoke_step["observation"] == {"exit_code": 0, "attempts": 4, "fixes": 3}
    statuses = [e[2]["status"] for e in bus.voyage_events if e[1] == "status"]
    assert "paused_ask" not in statuses  # 修复期间不打断
    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    assert resp.json()["status"] == "done"

async def test_setup_dep_failure_fixed_and_retried(client, queue_stub, fake_ssh, bus_recorder):
    """依赖安装第一次失败 → 报错回 LLM 修 requirements/run.sh → 重装通过（对称 smoke 自愈）。"""
    fake_ssh.setup_exits = [1, 0]
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    voyage = resp.json()
    assert voyage["status"] == "done", voyage
    setup_step = next(s for s in voyage["steps"] if s["action"] == "experiment.setup")
    obs = setup_step["observation"]
    assert obs["venv_exit"] == 0 and obs["attempts"] == 2 and obs["fixes"] == 1
    assert "\n".join(fake_ssh.commands).count("-r requirements.txt") == 2  # 首次 + 修复后重试

    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    assert resp.json()["status"] == "done"


async def test_setup_fixes_unbounded_until_success(client, queue_stub, fake_ssh, bus_recorder):
    """修复不再按次数设限（用户定调）：连续失败 3 次（超过旧上限 2）仍继续修，
    第 4 次装通、整条流水线走完，全程不提问。"""
    fake_ssh.setup_exits = [1, 1, 1, 0]
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    assert resp.json()["status"] == "done", resp.json()
    setup_step = next(s for s in resp.json()["steps"] if s["action"] == "experiment.setup")
    assert setup_step["observation"]["fixes"] == 3  # 旧上限是 2：现在不设限
    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    assert resp.json()["status"] == "done"


async def test_setup_time_budget_exceeded_asks(
    client, queue_stub, fake_ssh, bus_recorder, monkeypatch
):
    """唯一的刹车是时间预算：超出 budget.max_hours → 转向用户提问；
    回答重试后（时间预算内）装通走完。"""
    fake_ssh.setup_exits = [1, 0]
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(
        client, headers, project_id, idea_id, cred_id, budget={"max_hours": 2, "max_runs": 3}
    )
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    # 让「本阶段已耗时」显得超预算（相位时钟拨快）
    monkeypatch.setattr(ax, "_phase_deadline_exceeded", lambda budget, started: True)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    voyage = resp.json()
    assert voyage["status"] == "paused_ask", voyage
    ask = voyage["open_ask"]
    assert ask is not None and "超出时间预算" in ask["text"]
    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    assert resp.json()["status"] == "waiting_user"

    # 回答重试（恢复真实时钟）→ 装通走完
    monkeypatch.setattr(
        ax, "_phase_deadline_exceeded", ax._phase_deadline_exceeded.__wrapped__
        if hasattr(ax._phase_deadline_exceeded, "__wrapped__")
        else ax._phase_deadline_exceeded,
    )
    monkeypatch.undo()
    monkeypatch.setattr(ax, "RUN_POLL_SECONDS", 0)  # undo 会连 fast_poll 一起撤销，补回
    resp = await client.post(
        f"/api/voyages/{voyage_id}/asks/{ask['id']}/answer",
        json={"choice": "retry", "text": "换更快的镜像源"},
        headers=headers,
    )
    assert resp.status_code == 201
    await engine.resume(uuid.UUID(voyage_id))
    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    assert resp.json()["status"] == "done"


async def test_setup_fix_loop_picks_up_mid_action_suggestions(
    client, queue_stub, fake_ssh, bus_recorder
):
    """长动作期间的建议不再等下一个步骤边界：setup 修复循环每一轮实时拉取
    对话流新消息，注入修复 prompt 并标记消费（与步骤 params 持久化同事务）。"""
    from app.core.llm.base import CompletionResult
    from app.services import voyage_messages as messages_service

    fake_ssh.setup_exits = [1, 0]  # 失败一次 → 修复一轮（期间用户发建议）→ 装通

    class _FixPromptSpy(FakeProvider):
        def __init__(self) -> None:
            self.fix_prompts: list[str] = []

        async def complete(
            self, messages, *, model, temperature=0.7, max_tokens=None, images=None
        ) -> CompletionResult:
            full = "\n".join(m.content for m in messages)
            if "依赖安装退出码" in full:
                self.fix_prompts.append(full)
            return await super().complete(
                messages, model=model, temperature=temperature, max_tokens=max_tokens,
                images=images,
            )

    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    voyage_id = resp.json()["voyage_id"]

    posted = False

    async def post_suggestion(command: str) -> None:
        # 首次装依赖启动后（= setup 动作执行中途）用户发来建议
        nonlocal posted
        if "setup.exit" in command and not posted:
            posted = True
            async with get_sessionmaker()() as session:
                await messages_service.append_message(
                    session,
                    uuid.UUID(voyage_id),
                    role="user",
                    kind="chat",
                    text="换成 torch 的 cpu 版本",
                )

    fake_ssh.on_command = post_suggestion

    provider = _FixPromptSpy()
    router = LLMRouter()
    router.override_provider(provider)
    engine, _ = _make_engine(router)
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    assert posted
    assert provider.fix_prompts, "应发生过一次依赖修复"
    assert "换成 torch 的 cpu 版本" in provider.fix_prompts[0]
    assert "用户指示" in provider.fix_prompts[0]
    async with get_sessionmaker()() as session:
        pending = await messages_service.pending_chat_messages(session, uuid.UUID(voyage_id))
        assert pending == []  # 已消费，不残留


async def test_gate_approve_comment_flows_into_conversation(
    client, queue_stub, fake_ssh, bus_recorder
):
    """批准闸门时写的意见不再被丢弃：镜像成对话流建议，恢复执行后被消费。"""
    from app.services import voyage_messages as messages_service

    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    voyage_id = resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))  # 停在 compute_budget 闸门
    resp = await client.get(f"/api/gates?project_id={project_id}", headers=headers)
    gate = resp.json()[0]
    resp = await client.post(
        f"/api/gates/{gate['id']}/approve", json={"comment": "先用小模型跑"}, headers=headers
    )
    assert resp.status_code == 200

    resp = await client.get(f"/api/voyages/{voyage_id}/messages", headers=headers)
    chat = [m for m in resp.json() if m["kind"] == "chat"]
    assert any("先用小模型跑" in m["text"] for m in chat), chat

    await engine.resume(uuid.UUID(voyage_id))
    async with get_sessionmaker()() as session:
        pending = await messages_service.pending_chat_messages(session, uuid.UUID(voyage_id))
        assert pending == []  # 恢复执行后意见已注入决策点并标记消费


async def test_experiment_memory_written_and_synced(client, queue_stub, fake_ssh, bus_recorder):
    """实验记忆：关键事件确定性写入 checkpoint 镜像，并同步到 workdir/MEMORY.md；
    /code 端点可读（前端记忆页数据源）。"""
    fake_ssh.run_logs = [RUN_LOG, metric_log(0.75), metric_log(0.8)]
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(
        client, headers, project_id, idea_id, cred_id, budget={"max_hours": 2, "max_runs": 3}
    )
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, uuid.UUID(voyage_id))
        memory = (voyage.checkpoint or {}).get("memory_md") or ""
    assert "实验计划定稿" in memory
    assert "环境事实" in memory
    assert "第 1 轮结论" in memory and "第 3 轮结论" in memory
    assert "终止判定" in memory
    assert "报告" in memory
    # workdir 副本已同步（脚本可读、CodeTab 可见）；fake 按全路径存键
    memory_key = next(k for k in fake_ssh.files if k.endswith("/MEMORY.md"))
    remote = fake_ssh.files[memory_key]
    remote_text = remote.decode("utf-8") if isinstance(remote, bytes) else str(remote)
    assert "实验计划定稿" in remote_text
    # 前端记忆页数据源：/code/file 实时读
    resp = await client.get(
        f"/api/experiments/{exp_id}/code/file", params={"path": "MEMORY.md"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert "实验计划定稿" in resp.json()["content"]

    # 服务器不可达 → 回退 checkpoint 镜像（source=checkpoint）
    class _DeadConnector:
        async def connect(self, *a, **k):
            raise OSError("unreachable (test)")

    ssh_exec.set_connector_factory(lambda: _DeadConnector())
    try:
        resp = await client.get(
            f"/api/experiments/{exp_id}/code/file", params={"path": "MEMORY.md"}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["source"] == "checkpoint"
        assert "第 3 轮结论" in resp.json()["content"]
    finally:
        ssh_exec.set_connector_factory(None)


class _IntakeProvider(FakeProvider):
    """开题提问：返回 6 个问题（超上限，应被截到 5）。"""

    async def complete(self, messages, *, model, temperature=0.7, max_tokens=None, images=None):
        full = "\n".join(m.content for m in messages)
        if "开题助手" in full:
            payload = {
                "questions": [
                    {
                        "question": f"问题 {i}：用哪个数据集？",
                        "hint": f"提示 {i}",
                        "options": [f"候选 {i}A", f"候选 {i}B"],
                    }
                    for i in range(1, 7)
                ]
            }
            return CompletionResult(
                content=json.dumps(payload, ensure_ascii=False),
                model=model,
                finish_reason="stop",
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            )
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, images=images
        )


async def test_intake_questions_and_answers_reach_plan(client, queue_stub, fake_ssh, bus_recorder):
    """开题提问端点：按 idea 生成问题（≤5 截断）；回答随创建提交后进 plan prompt。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)

    from app.core.llm import router as llm_router_module

    router = llm_router_module.get_llm_router()
    router.override_provider(_IntakeProvider())
    try:
        resp = await client.post(
            f"/api/projects/{project_id}/experiments/intake-questions",
            json={"idea_id": idea_id},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        questions = resp.json()["questions"]
        assert len(questions) == 5  # 超出上限被截断
        assert questions[0]["question"].startswith("问题 1")
        assert questions[0]["hint"] == "提示 1"
        assert questions[0]["options"] == ["候选 1A", "候选 1B"]
    finally:
        llm_router_module.reset_llm_router()

    # 带开题问答创建：答案进 plan prompt（prompt spy）
    class _PlanPromptSpy(FakeProvider):
        def __init__(self) -> None:
            self.saw_intake = False

        async def complete(self, messages, *, model, temperature=0.7, max_tokens=None, images=None):
            full = "\n".join(m.content for m in messages)
            if "开题问答" in full and "用 GSM8K 的前 500 条" in full:
                self.saw_intake = True
            return await FakeProvider.complete(
                self, messages, model=model, temperature=temperature, max_tokens=max_tokens,
                images=images,
            )

    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    # 重建一个带 intake 的实验（上面的默认建实验只是确认互不影响）
    resp = await client.post(
        f"/api/projects/{project_id}/experiments",
        json={
            "idea_id": idea_id,
            "credential_id": cred_id,
            "params": {
                "intake": [
                    {"question": "用哪个数据集？", "answer": "用 GSM8K 的前 500 条"},
                    {"question": "要对照组吗？", "answer": ""},
                ]
            },
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    voyage_id = resp.json()["voyage_id"]
    spy = _PlanPromptSpy()
    router = LLMRouter()
    router.override_provider(spy)
    engine, _ = _make_engine(router)
    await engine.run(uuid.UUID(voyage_id))  # 跑到 compute_budget 闸门（plan 已生成）
    assert spy.saw_intake


def test_error_signature_normalizes():
    """同一错误不同行号/地址/路径 → 同签名；不同异常 → 不同签名。"""
    a = ax._error_signature(
        'File "/usr/local/lib/python3.12/dist-packages/x.py", line 1474, in _get\n'
        "RuntimeError: Failed to import transformers.training_args because of error 0x7f2a"
    )
    b = ax._error_signature(
        'File "/usr/local/lib/python3.12/dist-packages/x.py", line 99, in _get\n'
        "RuntimeError: Failed to import transformers.training_args because of error 0xdead"
    )
    assert a == b
    c = ax._error_signature("ImportError: No module named 'triton.backends'")
    assert c != a


def test_prompt_carries_stack_guard():
    """所有 codegen/修复 system prompt 常驻「预装环境保护」硬约束。"""
    ctx = ax.ActionContext(run=None, llm=None, checkpoint={"params": {}})
    out = ax._prompt_with_context(ax.CODE_SYSTEM_PROMPT, ctx)
    assert "预装环境保护" in out and "严禁重装" in out


async def test_smoke_same_error_escalates_then_asks(client, queue_stub, fake_ssh, bus_recorder):
    """零进展检测：同签名错误连发 → 第 2 次起修复 prompt 升级换根本策略，
    第 4 次转向用户提问（带签名与修复台账）；这不是次数上限。"""
    fake_ssh.smoke_exits = [1, 1, 1, 1]
    fake_ssh.smoke_stderr = "ImportError: cannot import name 'foo' from 'bar'"

    class _FixSpy(FakeProvider):
        def __init__(self) -> None:
            self.fix_prompts: list[str] = []

        async def complete(self, messages, *, model, temperature=0.7, max_tokens=None, images=None):
            full = "\n".join(m.content for m in messages)
            if "冒烟测试退出码" in full:
                self.fix_prompts.append(full)
            return await super().complete(
                messages, model=model, temperature=temperature, max_tokens=max_tokens,
                images=images,
            )

    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    voyage_id = resp.json()["voyage_id"]

    provider = _FixSpy()
    router = LLMRouter()
    router.override_provider(provider)
    engine, _ = _make_engine(router)
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    voyage = resp.json()
    assert voyage["status"] == "paused_ask", voyage
    ask = voyage["open_ask"]
    assert "同一个错误" in ask["text"]
    assert ask["payload"]["context"]["error_signature"]
    assert len(ask["payload"]["context"]["fix_ledger"]) == 3  # 三次修复全记了台账
    # 第 2/3 次修复 prompt 带升级指令；第 1 次不带
    assert len(provider.fix_prompts) == 3
    assert "禁止再微调版本号" not in provider.fix_prompts[0]
    assert "禁止再微调版本号" in provider.fix_prompts[1]
    assert "修复台账" in provider.fix_prompts[1]


async def test_smoke_different_errors_keep_fixing(client, queue_stub, fake_ssh, bus_recorder):
    """错误在变 = 有进展：连续 5 个不同错误不触发零进展提问，修到通过为止。"""
    fake_ssh.smoke_exits = [1, 1, 1, 1, 1, 0]
    attempt_no = 0

    kinds = ["Import", "Value", "Type", "Key", "Attribute"]

    async def rotate_error(command: str) -> None:
        nonlocal attempt_no
        if "--smoke" in command:
            attempt_no += 1
            kind = kinds[min(attempt_no, len(kinds)) - 1]
            fake_ssh.smoke_stderr = f"{kind}Error: failure in {kind.lower()} stage"

    fake_ssh.on_command = rotate_error
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    voyage_id = resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    assert resp.json()["status"] == "done", resp.json()
    smoke_step = next(s for s in resp.json()["steps"] if s["action"] == "experiment.smoke")
    assert smoke_step["observation"]["fixes"] == 5


async def test_setup_missing_local_resource_asks_once(
    client, queue_stub, fake_ssh, bus_recorder, monkeypatch
):
    """预检发现声明的本机模型不存在 → 主动提问（只问一次）；
    回答给出正确路径后 setup 继续走完，不再重复提问。"""

    async def fake_probe(executor, plan):
        return [], ["资源预检告警：声明的本机模型 ~/hf/model/x 不存在（找不到 config.json）。"]

    monkeypatch.setattr(ax, "_probe_resources", fake_probe)
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    voyage_id = resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    voyage = resp.json()
    assert voyage["status"] == "paused_ask", voyage
    ask = voyage["open_ask"]
    assert "本机资源不存在" in ask["text"]

    resp = await client.post(
        f"/api/voyages/{voyage_id}/asks/{ask['id']}/answer",
        json={"choice": "provide_path", "text": "模型在 /home/shenyl/hf/model/x"},
        headers=headers,
    )
    assert resp.status_code == 201
    await engine.resume(uuid.UUID(voyage_id))
    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    assert resp.json()["status"] == "done", resp.json()  # 只问一次，答后走完


def test_parse_gpu_csv_unit():
    """nvidia-smi CSV 解析：正常行解析、非法/短行跳过、空输入 → 空列表。"""
    text = "0, 49140, 48000\n1, 49140, 12000\nbad line\n2, x, y\n"
    gpus = ssh_exec.parse_gpu_csv(text)
    assert gpus == [
        {"index": 0, "mem_total_mib": 49140, "mem_free_mib": 48000},
        {"index": 1, "mem_total_mib": 49140, "mem_free_mib": 12000},
    ]
    assert ssh_exec.parse_gpu_csv("") == []


async def _seed_training_idea(project_id: str) -> str:
    """播一个「训练类」idea（含「训练」→ FakeProvider 产 kind=training 的 plan）。"""
    async with get_sessionmaker()() as session:
        idea = Idea(
            project_id=uuid.UUID(project_id),
            title="用 GRPO 训练小模型（test idea）",
            summary="训练一个 RL 方法提升数学推理",
            content="## 方法\n\n用强化学习训练模型。",
            status="promoted",
        )
        session.add(idea)
        await session.commit()
        return str(idea.id)


async def test_setup_gpu_preflight_passes_with_gpu(client, queue_stub, fake_ssh, bus_recorder):
    """训练类实验 + 本机有卡 → 资源预检通过，setup 观测记录 GPU，实验跑通。"""
    fake_ssh.gpus = [(0, 49140, 48000)]  # 一张空闲 A6000
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_training_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    voyage_id = resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    voyage = resp.json()
    assert voyage["status"] == "done", voyage
    setup_step = next(s for s in voyage["steps"] if s["action"] == "experiment.setup")
    assert setup_step["observation"]["gpus"] == [
        {"index": 0, "mem_total_mib": 49140, "mem_free_mib": 48000}
    ]


async def test_setup_gpu_preflight_warns_without_gpu(client, queue_stub, fake_ssh, bus_recorder):
    """训练类实验 + 本机探不到卡 → setup 观测里带**早期告警**（面板可见），但不硬停。
    （无 GPU 是基础设施问题，硬停会触发换方案重规划、抹掉失败步骤；换机拦截留后续一刀。）"""
    fake_ssh.gpus = []  # 无 GPU/驱动
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_training_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    voyage_id = resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    voyage = resp.json()
    setup_step = next(s for s in voyage["steps"] if s["action"] == "experiment.setup")
    obs = setup_step["observation"]
    assert obs["gpus"] == []  # 探不到卡如实记录
    assert any("资源预检告警" in w for w in obs.get("preflight_warnings", []))  # 早期告警已暴露


async def test_setup_no_gpu_preflight_silent_for_eval(client, queue_stub, fake_ssh, bus_recorder):
    """评测类实验不需要 GPU：探不到卡也不告警（needs_gpu=False）。"""
    fake_ssh.gpus = []
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)  # 默认 idea → kind=eval
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    voyage_id = resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    voyage = resp.json()
    setup_step = next(s for s in voyage["steps"] if s["action"] == "experiment.setup")
    assert "preflight_warnings" not in setup_step["observation"]  # 评测类不告警


def test_summarize_model_config_unit():
    """提取中性事实：model_type / architectures / config 分节；非法/缺字段 → 只取到的部分。"""
    facts = ax._summarize_model_config(
        json.dumps(
            {
                "model_type": "qwen3_5",
                "architectures": ["Qwen3_5ForConditionalGeneration"],
                "vision_config": {"depth": 32},
                "text_config": {"n": 1},
                "hidden_size": 4096,  # 非 *_config，不入 config_sections
            }
        )
    )
    assert facts == {
        "model_type": "qwen3_5",
        "architectures": ["Qwen3_5ForConditionalGeneration"],
        "config_sections": ["text_config", "vision_config"],
    }
    assert ax._summarize_model_config(json.dumps({"model_type": "qwen3"})) == {
        "model_type": "qwen3"
    }
    assert ax._summarize_model_config("not json at all") == {}


def test_validate_plan_models_normalization():
    """models 规范成 [{ref, role}]；注入型/空 ref 丢弃；非 list → 无 models。"""
    plan = ax.validate_plan(
        {
            "hypotheses": [{"text": "h", "status": "testing"}],
            "repro_strategy": "r",
            "steps": ["a", "b", "c"],
            "primary_metric": {"name": "acc", "direction": "maximize"},
            "budget_estimate": {"gpu_hours": 1},
            "models": [
                {"ref": "Qwen/Qwen3-1.7B", "role": "student"},
                {"ref": "~/hf/model/x", "role": "teacher"},
                {"ref": "evil; rm -rf /"},  # 注入 → 丢
                {"ref": "../escape"},  # .. → 丢
                {"role": "base"},  # 无 ref → 丢
            ],
        }
    )
    assert plan["models"] == [
        {"ref": "Qwen/Qwen3-1.7B", "role": "student"},
        {"ref": "~/hf/model/x", "role": "teacher"},
    ]


async def test_probe_resources_records_facts_and_missing(client, fake_ssh, bus_recorder):
    """通用预检：本机模型记录中性事实（model_type/架构/分节）；缺失 → 告警；HF id → remote 跳过。
    不下多模态/兼容判断——那是失败时诊断 LLM 的事，预检只摆事实。"""
    project_id, _ = await _setup_project(client)
    fake_ssh.host_files["~/hf/model/mm/config.json"] = json.dumps(
        {
            "model_type": "qwen3_5",
            "architectures": ["Qwen3_5ForConditionalGeneration"],
            "vision_config": {},
        }
    )
    executor = ssh_exec.SSHExecutor(
        FakeSSHSession(fake_ssh),
        exp_id=str(uuid.uuid4()),
        host="h",
        project_id=uuid.UUID(project_id),
    )
    plan = {
        "kind": "training",
        "models": [
            {"ref": "~/hf/model/mm", "role": "student"},  # 本机 → 记事实
            {"ref": "~/hf/model/missing", "role": "base"},  # 本机缺失 → 告警
            {"ref": "Qwen/Qwen3-1.7B", "role": "teacher"},  # HF id → remote
        ],
    }
    resources, warnings = await ax._probe_resources(executor, plan)
    by_ref = {r["ref"]: r for r in resources}
    assert by_ref["~/hf/model/mm"]["config"]["model_type"] == "qwen3_5"
    assert by_ref["~/hf/model/mm"]["config"]["config_sections"] == ["vision_config"]
    assert "multimodal" not in by_ref["~/hf/model/mm"]  # 不下判断
    assert by_ref["~/hf/model/missing"]["found"] is False
    assert by_ref["Qwen/Qwen3-1.7B"]["remote"] is True
    # 只对普适问题告警：声明的本机资源不存在（不针对具体架构/模态特判）
    assert any("不存在" in w and "~/hf/model/missing" in w for w in warnings)
    assert not any("多模态" in w for w in warnings)


async def test_budget_timeout_kills_run(client, queue_stub, fake_ssh, bus_recorder):
    """超 budget.max_hours → kill 远端进程 + run 置 failed；voyage 转提问、实验「等你回复」。"""
    fake_ssh.run_exit = None  # 进程一直不结束
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(
        client, headers, project_id, idea_id, cred_id, budget={"max_hours": 1e-9, "max_runs": 3}
    )
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    assert fake_ssh.pid in fake_ssh.killed
    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "waiting_user"  # 不再提前打死实验，镜像「等你回复」
    assert detail["runs"][0]["status"] == "failed"
    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    assert resp.json()["status"] == "paused_ask"  # run 级失败转提问，等人拍板
    assert resp.json()["open_ask"]["payload"]["ask_kind"] == "fatal_step"
    run_step = next(s for s in resp.json()["steps"] if s["action"] == "experiment.run")
    assert "max_hours" in run_step["observation"]["error"]


async def test_cancel_during_run_polling(client, queue_stub, fake_ssh, bus_recorder):
    """轮询循环内协作式取消：voyage 被置 cancelled → kill + run failed + 实验 cancelled。"""
    fake_ssh.run_exit = None  # 进程一直存活，直到被取消

    cancelled = False

    async def cancel_on_first_alive_check(command: str) -> None:
        nonlocal cancelled
        if "kill -0" in command and not cancelled:
            cancelled = True
            async with get_sessionmaker()() as session:
                run = (
                    await session.execute(select(VoyageRun).where(VoyageRun.kind == "experiment"))
                ).scalar_one()
                run.status = "cancelled"
                await session.commit()

    fake_ssh.on_command = cancel_on_first_alive_check

    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    engine, bus = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    assert cancelled
    assert fake_ssh.pid in fake_ssh.killed
    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "cancelled"
    assert detail["runs"][0]["status"] == "failed"
    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    assert resp.json()["status"] == "cancelled"
    exp_statuses = [m["status"] for _, m in bus.notify if m.get("type") == "experiment.status"]
    assert exp_statuses[-1] == "cancelled"


async def test_cancel_api(client, queue_stub, fake_ssh, bus_recorder):
    """cancel API：取消 voyage + kill 运行中的 PID + run 置 failed；重复取消 409。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    # 人工造一个"运行中"现场：exp running + 存活 run（pid 已知）
    async with get_sessionmaker()() as session:
        experiment = await session.get(Experiment, uuid.UUID(exp_id))
        experiment.status = "running"
        session.add(
            ExperimentRun(
                experiment_id=experiment.id,
                seq=1,
                command="bash run.sh",
                status="running",
                pid=31337,
            )
        )
        await session.commit()

    resp = await client.post(f"/api/experiments/{exp_id}/cancel", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert 31337 in fake_ssh.killed

    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, uuid.UUID(voyage_id))
        assert voyage.status == "cancelled"
        run = (
            await session.execute(
                select(ExperimentRun).where(ExperimentRun.experiment_id == uuid.UUID(exp_id))
            )
        ).scalar_one()
        assert run.status == "failed"
    # WS 事件
    assert any(
        m.get("type") == "experiment.status" and m["status"] == "cancelled"
        for _, m in bus_recorder.notify
    )

    # 终态实验重复取消 → 409
    resp = await client.post(f"/api/experiments/{exp_id}/cancel", headers=headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "EXPERIMENT_ALREADY_FINISHED"

    # 已取消的 voyage：引擎驱动是 no-op
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    assert resp.json()["status"] == "cancelled"


async def test_gate_reject_fails_experiment(client, queue_stub, fake_ssh, bus_recorder):
    """驳回 compute_budget 闸门 → voyage failed + 实验 failed（联动）。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    resp = await client.get(f"/api/gates?project_id={project_id}", headers=headers)
    gate = resp.json()[0]
    resp = await client.post(
        f"/api/gates/{gate['id']}/reject", json={"comment": "预算太高"}, headers=headers
    )
    assert resp.status_code == 200

    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    assert resp.json()["status"] == "failed"
    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    assert resp.json()["status"] == "failed"
    assert any(
        m.get("type") == "experiment.status" and m["status"] == "failed"
        for _, m in bus_recorder.notify
    )


class _PathViolationProvider(FakeProvider):
    """代码生成时故意产出 workdir 外路径，验证白名单拒绝。"""

    async def complete(self, messages, *, model, temperature=0.7, max_tokens=None):
        full = "\n".join(m.content for m in messages)
        if '"requirements.txt"' in full:
            content = json.dumps(
                {
                    "files": {
                        "../../etc/evil.py": "print('pwned')",
                        "requirements.txt": "",
                        "run.sh": "bash --smoke",
                    }
                }
            )
            return CompletionResult(
                content=content,
                model=model,
                finish_reason="stop",
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            )
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens
        )


async def test_path_violation_rejected(client, queue_stub, fake_ssh, bus_recorder):
    """LLM 产出 workdir 外路径 → SSHPathViolationError：不写任何文件（安全护栏），
    实验 failed；voyage 走 loop 失败回灌（重试 → 计划调整）仍无法推进 → 转提问等用户指示。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    router = LLMRouter()
    router.override_provider(_PathViolationProvider())
    engine, bus = _make_engine(router)
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}?include_obsolete=true", headers=headers)
    voyage = resp.json()
    assert voyage["status"] == "paused_ask"
    # fake navigator 的替代步骤能通过，最终停在完成标准终检的提问上
    # （真实场景会在更早的调整超限处停下，同样是 paused_ask）
    assert voyage["open_ask"]["payload"]["ask_kind"] == "done_criteria"
    setup_step = next(s for s in voyage["steps"] if s["action"] == "experiment.setup")
    assert "越界" in setup_step["observation"]["error"]
    assert setup_step["attempt"] == 2  # 执行类错误带诊断原地重试过一次
    statuses = [e[2]["status"] for e in bus.voyage_events if e[1] == "status"]
    assert "replanning" in statuses  # 重试尽后走了计划调整
    # 安全护栏的本质：越界文件绝不落盘（自愈后的合法平台文件如 MEMORY.md 允许写）
    assert not any("evil" in path for path in fake_ssh.files)
    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    assert resp.json()["status"] == "waiting_user"  # 提问中，未被提前打死


def test_relpath_whitelist_unit():
    """白名单路径校验单元测试。"""
    assert ssh_exec._validate_relpath("train.py") == "train.py"
    assert ssh_exec._validate_relpath("src/model.py") == "src/model.py"
    for bad in ("../evil.py", "/etc/passwd", "~/x", "a/../../b", "", "src/../../x"):
        with pytest.raises(ssh_exec.SSHPathViolationError):
            ssh_exec._validate_relpath(bad)
    with pytest.raises(ValueError):
        ssh_exec.workdir_for("$(rm -rf /)")  # exp_id 必须是 UUID


def test_validate_files_unit():
    ok = {"files": {"requirements.txt": "", "run.sh": "if --smoke", "train.py": "x = 1"}}
    assert set(ax.validate_files(ok)) == {"requirements.txt", "run.sh", "train.py"}
    with pytest.raises(ValueError):
        ax.validate_files({"files": {"run.sh": "--smoke"}})  # 缺 requirements.txt
    with pytest.raises(ValueError):
        ax.validate_files({"files": {"requirements.txt": "", "run.sh": "no smoke flag"}})


def test_validate_files_rejects_python_syntax_error():
    """生成的 .py 语法错必须在生成阶段打回，不能等到远端冒烟才炸。

    样本取自真实失败（voyage ae147dec）：f-string 里写了 ``\\"``。这个错以前一路穿过
    校验传到远端，直到冒烟才暴露，整个 voyage 判死。
    """
    broken = (
        'print(f"POLARIS_METRIC {json.dumps({\\"name\\": \\"acc\\", \\"value\\": v})}")\n'
    )
    with pytest.raises(ValueError) as exc:
        ax.validate_files(
            {"files": {"requirements.txt": "", "run.sh": "--smoke", "train.py": broken}}
        )
    # 报错要能指到文件与行号，模型才改得准
    assert "train.py" in str(exc.value)

    # 非 .py 文件不做语法检查：run.sh 里的 shell 片段不该被当成 Python
    ax.validate_files(
        {"files": {"requirements.txt": "", "run.sh": "if [ --smoke ]; then :; fi"}}
    )


def test_parse_metric_lines_unit():
    points = ax.parse_metric_lines(RUN_LOG + 'POLARIS_METRIC {"name": 1, "value": "x"}\n')
    assert points == [
        {"name": "accuracy", "step": 0, "value": 0.6},
        {"name": "accuracy", "step": 1, "value": 0.7},
        {"name": "loss", "step": 1, "value": 0.4},
    ]
    merged = ax.merge_metrics(None, points)
    assert set(merged) == {"accuracy", "loss"}


async def test_create_experiment_validation(client, queue_stub, fake_ssh):
    project_id, headers = await _setup_project(client)
    cred_id = await _create_credential(client, headers)

    # idea 未晋级 → 409
    candidate_id = await _seed_idea(project_id, status="candidate")
    resp = await _create_experiment(client, headers, project_id, candidate_id, cred_id)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "IDEA_NOT_PROMOTED"

    # idea 不存在 → 404
    resp = await _create_experiment(client, headers, project_id, str(uuid.uuid4()), cred_id)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "IDEA_NOT_FOUND"

    # 他人的凭据 → 404（不泄露存在性）
    _other_project, headers_b = await _setup_project(client, email="bob@example.com")
    cred_b = await _create_credential(client, headers_b)
    promoted_id = await _seed_idea(project_id)
    resp = await _create_experiment(client, headers, project_id, promoted_id, cred_b)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "CREDENTIAL_NOT_FOUND"

    # 默认预算：时数 0 = 无限时（用户定调），轮数与无提升阈值保底
    resp = await _create_experiment(client, headers, project_id, promoted_id, cred_id)
    assert resp.status_code == 201
    assert resp.json()["budget"] == {"max_hours": 0, "max_runs": 10, "no_improve_stop": 2}


async def test_experiment_member_permissions(client, queue_stub, fake_ssh):
    """非项目成员对 experiments 一律 404（不泄露存在性）。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id = resp.json()["id"]

    token_b = await register_and_login(client, email="outsider@example.com")
    headers_b = {"Authorization": f"Bearer {token_b}"}
    for method, url in (
        ("get", f"/api/projects/{project_id}/experiments"),
        ("post", f"/api/projects/{project_id}/experiments"),
        ("get", f"/api/experiments/{exp_id}"),
        ("post", f"/api/experiments/{exp_id}/cancel"),
        ("get", f"/api/experiments/{exp_id}/logs"),
        ("get", f"/api/experiments/{exp_id}/logs/stream"),
        ("get", f"/api/experiments/{exp_id}/figures/0/image"),
    ):
        kwargs = {"headers": headers_b}
        if method == "post" and url.endswith("/experiments"):
            kwargs["json"] = {"idea_id": idea_id, "credential_id": cred_id}
        resp = await getattr(client, method)(url, **kwargs)
        assert resp.status_code == 404, (method, url, resp.status_code)


async def test_code_browser_live_listing_and_read(client, queue_stub, fake_ssh, bus_recorder):
    """代码浏览：SSH 实时列 workdir 文件清单 + 读单文件内容；非法路径 400。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/experiments/{exp_id}/code", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "ssh"
    paths = [f["path"] for f in body["files"]]
    assert "run.sh" in paths and "train.py" in paths and "requirements.txt" in paths
    assert all(isinstance(f["size"], int) for f in body["files"])

    resp = await client.get(
        f"/api/experiments/{exp_id}/code/file", params={"path": "run.sh"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "ssh" and not body["binary"] and not body["truncated"]
    assert "--smoke" in body["content"]

    resp = await client.get(
        f"/api/experiments/{exp_id}/code/file", params={"path": "../evil"}, headers=headers
    )
    assert resp.status_code == 400
    resp = await client.get(
        f"/api/experiments/{exp_id}/code/file", params={"path": "nope.txt"}, headers=headers
    )
    assert resp.status_code == 404


async def test_code_browser_falls_back_to_checkpoint(client, queue_stub, fake_ssh, bus_recorder):
    """服务器不可达 → 代码浏览回退 voyage checkpoint 的 exp_files 快照。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    fake_ssh.connect_error = "host unreachable"  # 之后的连接全部失败
    resp = await client.get(f"/api/experiments/{exp_id}/code", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "checkpoint"
    paths = [f["path"] for f in body["files"]]
    assert "run.sh" in paths  # LLM 产出文件在快照里（平台注入文件不在，属预期）

    resp = await client.get(
        f"/api/experiments/{exp_id}/code/file", params={"path": "run.sh"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["source"] == "checkpoint"
    assert "--smoke" in resp.json()["content"]


async def test_experiment_sysinfo(client, queue_stub, fake_ssh, bus_recorder):
    """实验页系统状态：项目成员经实验拿所在服务器的 CPU/内存/磁盘/GPU（实时探测）。"""
    fake_ssh.gpus = [(0, 81920, 56000)]
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id = resp.json()["id"]

    resp = await client.get(f"/api/experiments/{exp_id}/sysinfo", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["cpu"]["cores"] == 64
    assert body["gpus"][0]["mem_total_mib"] == 81920


async def test_code_archive_and_single_download(client, queue_stub, fake_ssh, bus_recorder):
    """代码打包下载（zip 含全部文件）+ 单文件原样下载（attachment）。"""
    import io
    import zipfile

    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]
    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/experiments/{exp_id}/code/archive", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = zf.namelist()
    assert "run.sh" in names and "train.py" in names
    assert "--smoke" in zf.read("run.sh").decode()

    resp = await client.get(
        f"/api/experiments/{exp_id}/code/file/download",
        params={"path": "run.sh"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert 'filename="run.sh"' in resp.headers["content-disposition"]
    assert b"--smoke" in resp.content

    resp = await client.get(
        f"/api/experiments/{exp_id}/code/file/download",
        params={"path": "../evil"},
        headers=headers,
    )
    assert resp.status_code == 400


async def test_experiment_trash_flow(client):
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    ids = []
    async with get_sessionmaker()() as session:
        for _ in range(3):
            e = Experiment(
                project_id=uuid.UUID(project_id), idea_id=uuid.UUID(idea_id), status="done"
            )
            session.add(e)
            await session.flush()
            ids.append(str(e.id))
        await session.commit()

    async def active():
        r = await client.get(f"/api/projects/{project_id}/experiments", headers=headers)
        return {e["id"] for e in r.json()}

    async def trash():
        r = await client.get(
            f"/api/projects/{project_id}/experiments?trashed=true", headers=headers
        )
        return r.json()

    assert await active() == set(ids)
    # 批量移入垃圾箱 2 个
    r = await client.post(
        f"/api/projects/{project_id}/experiments/batch",
        json={"action": "trash", "ids": ids[:2]},
        headers=headers,
    )
    assert r.status_code == 200 and r.json()["affected"] == 2
    assert await active() == {ids[2]}
    t = await trash()
    assert {e["id"] for e in t} == set(ids[:2]) and all(e["trashed_at"] for e in t)
    # 恢复 1 个
    r = await client.post(f"/api/experiments/{ids[0]}/restore", headers=headers)
    assert r.status_code == 200 and r.json()["trashed_at"] is None
    assert ids[0] in await active()
    # 清空垃圾箱 → 永久删除仍在垃圾箱的 ids[1]
    r = await client.post(f"/api/projects/{project_id}/experiments/trash/empty", headers=headers)
    assert r.status_code == 200 and r.json()["affected"] == 1
    assert (await client.get(f"/api/experiments/{ids[1]}", headers=headers)).status_code == 404


async def test_complete_json_escalates_max_tokens_on_truncation():
    """截断（finish_reason=max_tokens）≠ 格式错：升档输出预算重试，不烧解析重试次数。"""
    from types import SimpleNamespace

    calls: list[int | None] = []

    class _TruncatingLLM:
        async def complete(self, stage, messages, **kw):
            calls.append(kw.get("max_tokens"))
            if len(calls) == 1:
                return SimpleNamespace(content='{"a": 1', finish_reason="max_tokens")
            return SimpleNamespace(content='{"a": 1}', finish_reason="stop")

    ctx = SimpleNamespace(
        llm=_TruncatingLLM(),
        run=SimpleNamespace(created_by=None, project_id=None, id=None),
    )
    out = await ax._complete_json(ctx, system="s", user="u", validate=lambda d: d)
    assert out == {"a": 1}
    assert calls == [None, 8192]  # 截断后第二次带升档 max_tokens
