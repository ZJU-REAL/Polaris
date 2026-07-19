"""M5-A 实验自动迭代 + 图表专项测试（docs/api-m5-a.md §6，离线 MockSSH + fake LLM）。

覆盖：早停（连续无提升 / 假设全部定论）、debug 独立限额、max_runs 截断、
no_improve_stop 预算入参、metrics.json 合并、figures 脚本失败与 VLM 质检失败的
修复重试、修复用尽降级、轮次开始协作式取消、plan/reflection/plot 校验单元测试。
3 轮 improve→improve→stop 全链路断言在 test_experiments.test_experiment_full_pipeline。
"""

import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.agents.voyage import actions_experiment as ax
from app.core.db import get_sessionmaker
from app.core.llm.base import CompletionResult
from app.core.llm.fake import FakeProvider
from app.core.llm.router import LLMRouter
from app.models.voyage import VoyageRun
from app.services import ssh_exec
from tests.fake_ssh import FakeSSHConnector, FakeSSHServer
from tests.test_experiments import (
    FAKE_PNG,
    RUN_LOG,
    _approve_gate,
    _create_credential,
    _create_experiment,
    _make_engine,
    _seed_idea,
    _setup_project,
    metric_log,
)


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


def _reflection_json(
    decision: str,
    *,
    updates: list[dict] | None = None,
    planned_change: str | None = "调大学习率（test）",
    stop_reason: str | None = None,
) -> str:
    return json.dumps(
        {
            "observation": f"观察（test，decision={decision}）",
            "diagnosis": "诊断（test）",
            "hypothesis_updates": updates
            if updates is not None
            else [{"index": 0, "status": "testing", "evidence": "仍在验证（test）"}],
            "decision": decision,
            "planned_change": planned_change,
            "stop_reason": stop_reason,
        },
        ensure_ascii=False,
    )


def _result(content: str, model: str) -> CompletionResult:
    return CompletionResult(
        content=content,
        model=model,
        finish_reason="stop",
        usage={"prompt_tokens": 1, "completion_tokens": 1},
    )


class _FixedReflectionProvider(FakeProvider):
    """reflection 一律返回固定 decision（improve/debug），其余请求走 FakeProvider。"""

    def __init__(self, decision: str, updates: list[dict] | None = None) -> None:
        self._decision = decision
        self._updates = updates

    async def complete(
        self, messages, *, model, temperature=0.7, max_tokens=None, images=None
    ) -> CompletionResult:
        full = "\n".join(m.content for m in messages)
        if not images and '"hypothesis_updates"' in full:
            return _result(_reflection_json(self._decision, updates=self._updates), model)
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, images=images
        )


class _QCFailOnceProvider(FakeProvider):
    """VLM 质检第一次不合格（issues），第二次合格并给图注；其余走 FakeProvider。"""

    def __init__(self) -> None:
        self.qc_calls = 0

    async def complete(
        self, messages, *, model, temperature=0.7, max_tokens=None, images=None
    ) -> CompletionResult:
        full = "\n".join(m.content for m in messages)
        if images and "图表质检员" in full:
            self.qc_calls += 1
            if self.qc_calls == 1:
                payload = {"passed": False, "figures": [], "issues": ["缺少图例（test）"]}
            else:
                payload = {
                    "passed": True,
                    "figures": [{"index": 0, "caption": "修复后的主指标图（test）"}],
                    "issues": [],
                }
            return _result(json.dumps(payload, ensure_ascii=False), model)
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, images=images
        )


def _router_with(provider: FakeProvider) -> LLMRouter:
    router = LLMRouter()
    router._providers[("fake", None, "")] = provider
    return router


async def _launch_experiment(client, budget=None):
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    cred_id = await _create_credential(client, headers)
    resp = await _create_experiment(client, headers, project_id, idea_id, cred_id, budget=budget)
    assert resp.status_code == 201, resp.text
    return project_id, headers, resp.json()["id"], resp.json()["voyage_id"]


async def _drive_pipeline(client, headers, project_id, voyage_id, router=None):
    engine, bus = _make_engine(router)
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    await engine.resume(uuid.UUID(voyage_id))
    return bus


async def _get_detail(client, headers, exp_id):
    resp = await client.get(f"/api/experiments/{exp_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _iterate_observation(client, headers, voyage_id):
    """末个 analyze 节点的 observation（迭代终止判定所在，docs/voyage-loop.md §7）。"""
    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    step = next(s for s in reversed(resp.json()["steps"]) if s["action"] == "experiment.analyze")
    return resp.json()["status"], step["observation"]


# ---- 早停与终止条件 ----


async def test_no_improve_early_stop(client, queue_stub, fake_ssh, bus_recorder):
    """连续 2 轮主指标无提升自动停（decision 一直 improve 也拦得住）。"""
    # 每轮日志相同 → 主指标恒 0.7：r1 建基线、r2 streak=1、r3 streak=2 → 停
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    await _drive_pipeline(
        client, headers, project_id, voyage_id, _router_with(_FixedReflectionProvider("improve"))
    )

    voyage_status, observation = await _iterate_observation(client, headers, voyage_id)
    assert voyage_status == "done"
    assert observation["stopped_reason"] == "no_improve"
    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"
    assert len(detail["runs"]) == 3
    assert [r["primary_value"] for r in detail["runs"]] == [0.7, 0.7, 0.7]
    assert detail["iteration_state"] == {
        "no_improve_streak": 2,
        "debug_count": 0,
        "stopped_reason": "no_improve",
    }


async def test_no_improve_stop_budget_param(client, queue_stub, fake_ssh, bus_recorder):
    """budget.no_improve_stop 入参可调（3 → 多容忍一轮才停）。"""
    project_id, headers, exp_id, voyage_id = await _launch_experiment(
        client, budget={"max_hours": 2, "max_runs": 10, "no_improve_stop": 3}
    )
    detail = await _get_detail(client, headers, exp_id)
    assert detail["budget"] == {"max_hours": 2, "max_runs": 10, "no_improve_stop": 3}
    await _drive_pipeline(
        client, headers, project_id, voyage_id, _router_with(_FixedReflectionProvider("improve"))
    )

    detail = await _get_detail(client, headers, exp_id)
    assert len(detail["runs"]) == 4  # 基线 + 3 轮无提升
    assert detail["iteration_state"]["stopped_reason"] == "no_improve"
    assert detail["iteration_state"]["no_improve_streak"] == 3


async def test_debug_limit_terminates(client, queue_stub, fake_ssh, bus_recorder):
    """运行失败走 debug 分支：独立限额 3 次，第 4 次 debug 决策直接终止。"""
    fake_ssh.run_exit = 1  # 每轮正式运行都失败
    fake_ssh.run_log = "Traceback: train boom\n"
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    await _drive_pipeline(
        client, headers, project_id, voyage_id, _router_with(_FixedReflectionProvider("debug"))
    )

    voyage_status, observation = await _iterate_observation(client, headers, voyage_id)
    assert voyage_status == "done"
    assert observation["stopped_reason"] == "debug_limit"
    detail = await _get_detail(client, headers, exp_id)
    assert len(detail["runs"]) == 4  # 首轮 + 3 次 debug 修复重跑
    assert all(r["status"] == "failed" for r in detail["runs"])
    assert all(r["reflection"]["decision"] == "debug" for r in detail["runs"])
    assert detail["iteration_state"] == {
        "no_improve_streak": 0,  # 失败轮无主指标，不计入无提升
        "debug_count": 3,
        "stopped_reason": "debug_limit",
    }
    # 末轮 failed → 报告收口为 failed
    assert detail["status"] == "failed"
    assert detail["report"].startswith("## 实验报告")


async def test_max_runs_truncates_iteration(client, queue_stub, fake_ssh, bus_recorder):
    """达 budget.max_runs 截断（指标仍在提升、decision 一直 improve）。"""
    fake_ssh.run_logs = [metric_log(0.7), metric_log(0.8)]
    project_id, headers, exp_id, voyage_id = await _launch_experiment(
        client, budget={"max_hours": 2, "max_runs": 2}
    )
    await _drive_pipeline(
        client, headers, project_id, voyage_id, _router_with(_FixedReflectionProvider("improve"))
    )

    _, observation = await _iterate_observation(client, headers, voyage_id)
    assert observation["stopped_reason"] == "max_runs"
    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"
    assert [r["primary_value"] for r in detail["runs"]] == [0.7, 0.8]
    assert detail["iteration_state"]["stopped_reason"] == "max_runs"


async def test_all_hypotheses_resolved_stops(client, queue_stub, fake_ssh, bus_recorder):
    """假设全部非 testing 即停（哪怕 decision=improve），回写含 evidence。"""
    updates = [
        {"index": 0, "status": "verified", "evidence": "证据 A（test）"},
        {"index": 1, "status": "falsified", "evidence": "证据 B（test）"},
    ]
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    await _drive_pipeline(
        client,
        headers,
        project_id,
        voyage_id,
        _router_with(_FixedReflectionProvider("improve", updates=updates)),
    )

    _, observation = await _iterate_observation(client, headers, voyage_id)
    assert observation["stopped_reason"] == "hypotheses_resolved"
    detail = await _get_detail(client, headers, exp_id)
    assert len(detail["runs"]) == 1
    hyps = detail["plan"]["hypotheses"]
    assert [h["status"] for h in hyps] == ["verified", "falsified"]
    assert [h["evidence"] for h in hyps] == ["证据 A（test）", "证据 B（test）"]


async def test_metrics_json_merged_into_primary_value(client, queue_stub, fake_ssh, bus_recorder):
    """可选 workdir/metrics.json 与 POLARIS_METRIC 合并，主指标取合并后末值。"""
    fake_ssh.metrics_json = json.dumps({"f1": 0.5, "accuracy": [{"step": 99, "value": 0.9}]})
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    await _drive_pipeline(client, headers, project_id, voyage_id)  # 默认 improve→improve→stop

    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"
    run = detail["runs"][0]
    assert run["metrics"]["f1"] == [{"step": None, "value": 0.5}]
    assert run["metrics"]["accuracy"][-1] == {"step": 99, "value": 0.9}
    assert all(r["primary_value"] == 0.9 for r in detail["runs"])
    assert detail["metrics"]["f1"]  # 汇总指标同步合并


async def test_cancel_at_round_start(client, queue_stub, fake_ssh, bus_recorder):
    """每轮开始的协作式 cancel 检查：第 1 轮结束后取消 → 不再启动第 2 轮。"""
    cancelled = False

    async def cancel_after_round_one(command: str) -> None:
        nonlocal cancelled
        # read_metrics_json 发生在本轮 poll 结束后、reflection 之前
        if command.startswith("cat") and "/metrics.json" in command and not cancelled:
            cancelled = True
            async with get_sessionmaker()() as session:
                run = (
                    await session.execute(select(VoyageRun).where(VoyageRun.kind == "experiment"))
                ).scalar_one()
                run.status = "cancelled"
                await session.commit()

    fake_ssh.on_command = cancel_after_round_one
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    await _drive_pipeline(
        client, headers, project_id, voyage_id, _router_with(_FixedReflectionProvider("improve"))
    )

    assert cancelled
    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "cancelled"
    assert len(detail["runs"]) == 1  # 第 2 轮没有启动
    assert detail["runs"][0]["status"] == "succeeded"
    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    assert resp.json()["status"] == "cancelled"
    assert "\n".join(fake_ssh.commands).count("nohup") == 1


# ---- figures 步骤 ----


async def test_figures_qc_failure_retries_script(client, queue_stub, fake_ssh, bus_recorder):
    """VLM 质检不合格 → issues 回 LLM 重生成脚本重跑 → 二次质检通过。"""
    provider = _QCFailOnceProvider()
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    await _drive_pipeline(client, headers, project_id, voyage_id, _router_with(provider))

    assert provider.qc_calls == 2
    assert "\n".join(fake_ssh.commands).count(".venv/bin/python plot_figures.py") == 2
    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    figures_step = next(s for s in resp.json()["steps"] if s["action"] == "experiment.figures")
    assert figures_step["observation"] == {"figures": 1, "qc_passed": True, "fixes": 1}
    detail = await _get_detail(client, headers, exp_id)
    assert detail["figures"] == [
        {"index": 0, "name": "primary_metric.png", "caption": "修复后的主指标图（test）"}
    ]
    # 图片端点可取（同名 pdf 一并拉回本地供论文用）
    resp = await client.get(f"/api/experiments/{exp_id}/figures/0/image", headers=headers)
    assert resp.status_code == 200
    assert resp.content == FAKE_PNG
    from app.services import experiments as experiments_service

    assert experiments_service.figure_local_path(exp_id, "primary_metric.pdf").is_file()


async def test_figures_script_error_retries(client, queue_stub, fake_ssh, bus_recorder):
    """绘图脚本执行失败（exit≠0）→ stderr 回 LLM 修脚本 → 重跑成功。"""
    fake_ssh.plot_exits = [1, 0]
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    await _drive_pipeline(client, headers, project_id, voyage_id)

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    figures_step = next(s for s in resp.json()["steps"] if s["action"] == "experiment.figures")
    assert figures_step["observation"] == {"figures": 1, "qc_passed": True, "fixes": 1}
    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"
    assert len(detail["figures"]) == 1


async def test_figures_fixes_exhausted_degrades(client, queue_stub, fake_ssh, bus_recorder):
    """脚本连续失败超限（首跑 + 2 次修复）→ 降级空 figures，不阻塞报告。"""
    fake_ssh.plot_exits = [1, 1, 1]
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    await _drive_pipeline(client, headers, project_id, voyage_id)

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    assert resp.json()["status"] == "done"
    figures_step = next(s for s in resp.json()["steps"] if s["action"] == "experiment.figures")
    assert figures_step["observation"]["figures"] == 0
    assert figures_step["observation"]["qc_passed"] is False
    assert figures_step["observation"]["fixes"] == 2
    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"  # 图表降级不影响报告收口
    assert detail["figures"] == []
    resp = await client.get(f"/api/experiments/{exp_id}/figures/0/image", headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "FIGURE_NOT_FOUND"


# ---- 校验单元测试 ----


def _valid_plan() -> dict:
    return {
        "hypotheses": [{"text": "h1", "status": "testing"}],
        "repro_strategy": "官方代码复现",
        "steps": ["s1", "s2", "s3"],
        "primary_metric": {"name": "accuracy", "direction": "maximize"},
        "budget_estimate": {"gpu_hours": 2},
    }


def test_validate_plan_requires_primary_metric():
    plan = ax.validate_plan(_valid_plan())
    assert plan["primary_metric"] == {"name": "accuracy", "direction": "maximize"}

    missing = _valid_plan()
    del missing["primary_metric"]
    with pytest.raises(ValueError, match="primary_metric"):
        ax.validate_plan(missing)

    bad_direction = _valid_plan()
    bad_direction["primary_metric"] = {"name": "accuracy", "direction": "up"}
    with pytest.raises(ValueError, match="direction"):
        ax.validate_plan(bad_direction)

    no_name = _valid_plan()
    no_name["primary_metric"] = {"direction": "minimize"}
    with pytest.raises(ValueError, match="name"):
        ax.validate_plan(no_name)


def test_validate_reflection_unit():
    ok = ax.validate_reflection(
        json.loads(_reflection_json("stop", stop_reason="足够了", planned_change=None))
    )
    assert ok["decision"] == "stop"
    assert ok["stop_reason"] == "足够了"
    assert ok["hypothesis_updates"][0] == {
        "index": 0,
        "status": "testing",
        "evidence": "仍在验证（test）",
    }

    for mutate in (
        {"decision": "retry"},  # 非法 decision
        {"observation": ""},  # 空 observation
        {"hypothesis_updates": [{"index": "0", "status": "testing"}]},  # index 非 int
        {"hypothesis_updates": [{"index": 0, "status": "maybe"}]},  # 非法 status
    ):
        payload = json.loads(_reflection_json("improve")) | mutate
        with pytest.raises((ValueError, TypeError)):
            ax.validate_reflection(payload)


def test_validate_plot_files_unit():
    ok = ax.validate_plot_files(
        {"files": {"plot_figures.py": "data = json.load(open('metrics_all.json'))"}}
    )
    assert set(ok) == {"plot_figures.py"}
    with pytest.raises(ValueError, match="plot_figures.py"):
        ax.validate_plot_files({"files": {"draw.py": "print(1)"}})
    with pytest.raises(ValueError, match="metrics_all.json"):
        ax.validate_plot_files({"files": {"plot_figures.py": "print('hardcoded')"}})


def test_primary_value_and_improvement_unit():
    metrics = {"accuracy": [{"step": 0, "value": 0.6}, {"step": 1, "value": 0.7}]}
    assert ax.extract_primary_value(metrics, "accuracy") == 0.7
    assert ax.extract_primary_value(metrics, "loss") is None
    assert ax.extract_primary_value(None, "accuracy") is None
    # direction 感知比较
    assert ax.is_improvement(0.8, 0.7, "maximize")
    assert not ax.is_improvement(0.7, 0.7, "maximize")
    assert ax.is_improvement(0.3, 0.4, "minimize")
    assert not ax.is_improvement(0.5, 0.4, "minimize")
    assert ax.is_improvement(0.1, None, "maximize")
    # metrics.json 两种形态解析
    points = ax.parse_metrics_json('{"f1": 0.5, "acc": [{"step": 1, "value": 0.9}]}')
    assert points == [
        {"name": "f1", "step": None, "value": 0.5},
        {"name": "acc", "step": 1, "value": 0.9},
    ]
    assert ax.parse_metrics_json("not json") == []
    assert ax.parse_metrics_json('["list"]') == []


async def test_poll_survives_ssh_reconnect(client, queue_stub, fake_ssh, bus_recorder, monkeypatch):
    """轮询期间 SSH 瞬时断开 → 重连后继续跟踪，实验不因单次断连而失败。

    复现并回归线上 bug：一次 ChannelOpenError 曾直接把实验判 failed，而 nohup 脱离
    会话的远端进程其实还在跑（run.exit/run.log/pid 都持久化在服务器）。"""
    from app.models.activity import Activity

    monkeypatch.setattr(ax, "_reconnect_backoff", lambda _s: 0.0)  # 零退避，测试不等

    state = {"raised": 0}

    async def drop_once(command: str) -> None:
        # 第一轮正式运行读取退出码时断连一次（cat run.exit），之后恢复
        if command.startswith("cat") and "run.exit" in command and state["raised"] == 0:
            state["raised"] += 1
            raise ConnectionError("SSH connection closed")

    fake_ssh.on_command = drop_once

    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    await _drive_pipeline(
        client, headers, project_id, voyage_id, _router_with(_FixedReflectionProvider("improve"))
    )

    voyage_status, _ = await _iterate_observation(client, headers, voyage_id)
    assert voyage_status == "done"  # 断连没有毒死航程
    assert state["raised"] == 1  # 确实注入了一次断连

    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"
    assert detail["runs"], "应有成功的运行记录"
    assert all(r["status"] == "succeeded" for r in detail["runs"])  # 断连轮也成功收尾

    # 重连事件被审计；连接器被重新调用（connects ≥ 2）
    assert len(fake_ssh.connects) >= 2
    async with get_sessionmaker()() as session:
        stmt = select(Activity.kind).where(Activity.kind == "experiment.ssh_reconnect")
        kinds = (await session.execute(stmt)).scalars().all()
    assert kinds, "应记录 experiment.ssh_reconnect 审计活动"


def test_render_attempt_archive_unit():
    """通用「先验经验档案」渲染：列出各尝试的得分/源码/轨迹，标注迄今最好。"""
    archive = [
        {"seq": 1, "primary_value": 0.60, "files": {"train.py": "A_CODE"}, "trace": "log-a"},
        {"seq": 2, "primary_value": 0.72, "files": {"train.py": "B_CODE"}, "trace": "log-b"},
    ]
    text = ax._render_attempt_archive(archive)
    assert "历史尝试档案" in text and "共 2 次" in text
    assert "seq=1" in text and "seq=2" in text
    assert "0.6" in text and "0.72" in text
    assert "A_CODE" in text and "B_CODE" in text  # 全量源码，非压缩反馈
    assert text.count("★迄今最好") == 1 and "seq=2 | 主指标=0.72 ★迄今最好" in text
    assert ax._render_attempt_archive([]) == ""  # 空档案不产出


class _RecordingImproveProvider(_FixedReflectionProvider):
    """reflection 恒 improve，并记录喂给迭代 proposer 的（含档案的）改进提示。"""

    def __init__(self) -> None:
        super().__init__("improve")
        self.improve_prompts: list[str] = []

    async def complete(self, messages, *, model, temperature=0.7, max_tokens=None, images=None):
        full = "\n".join(m.content for m in messages)
        if "历史尝试档案" in full:  # 只有迭代优化 proposer 的 user prompt 才带档案
            self.improve_prompts.append(full)
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, images=images
        )


async def test_improve_proposer_gets_full_attempt_archive(
    client, queue_stub, fake_ssh, bus_recorder
):
    """通用能力：迭代改进的 proposer 拿到的是**全量历史尝试**（源码+得分+轨迹），不是只有上一轮。"""
    fake_ssh.run_logs = [metric_log(0.7), metric_log(0.8)]
    provider = _RecordingImproveProvider()
    project_id, headers, exp_id, voyage_id = await _launch_experiment(
        client, budget={"max_hours": 2, "max_runs": 2}
    )
    await _drive_pipeline(client, headers, project_id, voyage_id, _router_with(provider))

    assert provider.improve_prompts, "迭代改进应把历史尝试档案喂给 proposer"
    first = provider.improve_prompts[0]
    assert "历史尝试档案" in first
    assert "seq=1" in first and "0.7" in first  # 上一轮尝试的得分在档案里


async def test_smoke_timeout_is_recoverable(client, queue_stub, fake_ssh, bus_recorder):
    """冒烟超时不再硬崩：诊断为『太慢/超时』→ 自动改小重试 → 通过，航程继续。

    回归自适应循环的一类失败：训练类实验冒烟常因规模太大而超时(TimeoutError),
    以前直接判失败;现在当作可修失败,重连+让 LLM 把冒烟改小再试。"""
    state = {"raised": 0}

    async def timeout_once(command: str) -> None:
        if "--smoke" in command and state["raised"] == 0:
            state["raised"] += 1
            raise TimeoutError("smoke timed out")

    fake_ssh.on_command = timeout_once
    project_id, headers, exp_id, voyage_id = await _launch_experiment(
        client, budget={"max_hours": 2, "max_runs": 1}
    )
    await _drive_pipeline(
        client, headers, project_id, voyage_id, _router_with(_FixedReflectionProvider("improve"))
    )

    assert state["raised"] == 1  # 确实注入了一次冒烟超时
    voyage_status, _ = await _iterate_observation(client, headers, voyage_id)
    assert voyage_status == "done"  # 没在 smoke 硬崩,跑到 analyze 并收尾

    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers)
    smoke = next(s for s in resp.json()["steps"] if s["action"] == "experiment.smoke")
    assert smoke["status"] == "passed"
    assert (smoke["observation"] or {}).get("fixes", 0) >= 1  # 记录了一次方案级修复
    assert len(fake_ssh.connects) >= 2  # 超时后重连过
