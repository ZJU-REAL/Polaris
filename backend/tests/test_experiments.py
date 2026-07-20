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
    assert "\n".join(fake_ssh.commands).count("pip install") == 2  # 首次 + 修复后重试

    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    assert resp.json()["status"] == "done"


async def test_setup_deps_escalate_not_hard_fail(client, queue_stub, fake_ssh, bus_recorder):
    """依赖装不上、内部修复用尽 → setup 不像 smoke 那样硬停：它是「可换方案」节点，
    升级到引擎级重试/AI 计划调整（navigator：setup 走 loop 回灌）。本例重试后恢复 → voyage done。"""
    fake_ssh.setup_exits = [1, 1, 1]  # 够耗尽一轮内部修复（1 次 + 2 次 fixes）后升级
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
    # 关键区别：内部修复用尽后 setup 不硬停，升级重试后恢复完成（对比 smoke 用尽即 failed）
    assert voyage["status"] == "done", voyage
    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    assert resp.json()["status"] == "done"


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
    实验 failed；voyage 走 loop 失败回灌（重试 → 计划调整）仍无法推进 → paused_error 等人工。"""
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id)
    exp_id, voyage_id = resp.json()["id"], resp.json()["voyage_id"]

    router = LLMRouter()
    router._providers[("fake", None, "")] = _PathViolationProvider()
    engine, bus = _make_engine(router)
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))

    resp = await client.get(f"/api/voyages/{voyage_id}?include_obsolete=true", headers=headers)
    voyage = resp.json()
    assert voyage["status"] == "paused_error"
    setup_step = next(s for s in voyage["steps"] if s["action"] == "experiment.setup")
    assert "越界" in setup_step["observation"]["error"]
    assert setup_step["attempt"] == 2  # 执行类错误带诊断原地重试过一次
    statuses = [e[2]["status"] for e in bus.voyage_events if e[1] == "status"]
    assert "replanning" in statuses  # 重试尽后走了计划调整
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
