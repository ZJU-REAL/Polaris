"""Experiment Lab 全流程测试：fake LLM + MockSSH 全离线，直接驱动 VoyageEngine。

覆盖：plan→gate→approve→setup→smoke（失败修复重试）→iterate（3 轮 improve/stop）→
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
from tests.fake_ssh import FakeSSHConnector, FakeSSHServer
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

    # 阶段二：setup → smoke → iterate → figures → report → done
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    voyage = resp.json()
    assert voyage["status"] == "done", voyage
    assert [s["status"] for s in voyage["steps"]] == ["passed"] * 6
    assert [s["action"] for s in voyage["steps"]] == [
        "experiment.plan",
        "experiment.setup",
        "experiment.smoke",
        "experiment.iterate",
        "experiment.figures",
        "experiment.report",
    ]
    iterate_step = voyage["steps"][3]
    assert iterate_step["observation"]["rounds"] == 3
    assert iterate_step["observation"]["stopped_reason"] == "假设已全部有结论（fake）"

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
    assert joined.count("nohup") == 3  # 3 轮 launch
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


async def test_smoke_exhausted_fails_experiment(client, queue_stub, fake_ssh, bus_recorder):
    """冒烟连续失败（1 次 + 2 次修复重试）→ 实验 failed，固定管线不重规划，voyage failed。"""
    fake_ssh.smoke_exits = [1, 1, 1]
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
    assert voyage["status"] == "failed"
    statuses = [e[2]["status"] for e in bus.voyage_events if e[1] == "status"]
    assert "replanning" not in statuses  # on_failure=fail：不走 LLM 重规划
    smoke_step = voyage["steps"][2]
    assert "冒烟测试连续失败" in smoke_step["observation"]["error"]

    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    assert resp.json()["status"] == "failed"


async def test_budget_timeout_kills_run(client, queue_stub, fake_ssh, bus_recorder):
    """超 budget.max_hours → kill 远端进程 + run/experiment/voyage 置 failed。"""
    fake_ssh.run_exit = None  # 进程一直不结束
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(
        client, headers, project_id, idea_id, cred_id, budget={"max_hours": 0, "max_runs": 3}
    )
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    engine, _ = _make_engine()
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    assert fake_ssh.pid in fake_ssh.killed
    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    detail = resp.json()
    assert detail["status"] == "failed"
    assert detail["runs"][0]["status"] == "failed"
    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    assert resp.json()["status"] == "failed"
    run_step = next(s for s in resp.json()["steps"] if s["action"] == "experiment.iterate")
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
    """LLM 产出 workdir 外路径 → SSHPathViolationError，不重试、不写任何文件，实验 failed。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    router = LLMRouter()
    router._providers[("fake", None, "")] = _PathViolationProvider()
    engine, _ = _make_engine(router)
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    voyage = resp.json()
    assert voyage["status"] == "failed"
    setup_step = next(s for s in voyage["steps"] if s["action"] == "experiment.setup")
    assert "越界" in setup_step["observation"]["error"]
    assert fake_ssh.files == {}  # 一个文件都没写出去
    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    assert resp.json()["status"] == "failed"


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
    ok = {"files": {"requirements.txt": "", "run.sh": "if --smoke", "train.py": "x"}}
    assert set(ax.validate_files(ok)) == {"requirements.txt", "run.sh", "train.py"}
    with pytest.raises(ValueError):
        ax.validate_files({"files": {"run.sh": "--smoke"}})  # 缺 requirements.txt
    with pytest.raises(ValueError):
        ax.validate_files({"files": {"requirements.txt": "", "run.sh": "no smoke flag"}})


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

    # 默认预算（M5-A：含 no_improve_stop）
    resp = await _create_experiment(client, headers, project_id, promoted_id, cred_id)
    assert resp.status_code == 201
    assert resp.json()["budget"] == {"max_hours": 4, "max_runs": 10, "no_improve_stop": 2}


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
