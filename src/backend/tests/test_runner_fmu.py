"""fmu 后端测试（#682）：manifest/注册表/可用性守卫/spec 校验/collect 解析/worker。

分层策略（fmpy 与真 FMU 都是可选的，测试不因环境缺料而假红）：
- 静态层（大多数用例）：fmpy_available/_read_model_description 打桩，不需要 fmpy，
  也不需要真 FMU——validate 的全部分支、collect 的 csv 解析、poll/cancel 语义；
- worker 层：真起子进程跑 fmu_worker 的失败路径（读不到 sim.json），不需要 fmpy；
- 端到端层：真 fmpy + tests/fixtures/BouncingBall.fmu（Reference-FMUs，2-Clause BSD）
  validate→prepare→launch→poll→collect 全周期，环境缺 fmpy/FMU 二进制不支持本平台
  时 skip（不是 fail）。
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.schemas.experiment import ExperimentParams
from app.services.runners import contract, fmu, registry
from app.services.runners.fmu import FMU_MANIFEST, FMURunner

FIXTURES = Path(__file__).parent / "fixtures"
BOUNCING_BALL = FIXTURES / "BouncingBall.fmu"

GOOD_SPEC = {
    "stop_time": 3.0,
    "step_size": 0.1,
    "output_variables": ["h", "v"],
    "parameters": {"e": 0.7},
}

# 打桩用的 modelDescription：变量表与 BouncingBall 一致
FAKE_MD = SimpleNamespace(
    fmiVersion="2.0",
    modelVariables=[SimpleNamespace(name=n) for n in ("time", "h", "v", "g", "e")],
)


@pytest.fixture
def stub_fmpy(monkeypatch):
    """静态层打桩：fmpy 视为可用，modelDescription 用假的（不解 zip 不 import fmpy）。"""
    monkeypatch.setattr(fmu, "fmpy_available", lambda: True)
    monkeypatch.setattr(fmu, "_read_model_description", lambda path: FAKE_MD)


def make_workdir(tmp_path: Path, spec=GOOD_SPEC, with_fmu: bool = True) -> Path:
    workdir = tmp_path / "run"
    workdir.mkdir(parents=True)
    if with_fmu:
        (workdir / "model.fmu").write_bytes(b"PK\x03\x04 fake zip")
    if spec is not None:
        text = spec if isinstance(spec, str) else json.dumps(spec)
        (workdir / "sim.json").write_text(text, encoding="utf-8")
    return workdir


# ---- manifest 与注册表 ----


def test_fmu_manifest_declares_pure_batch_simulation():
    m = FMU_MANIFEST
    assert m.backend == "fmu"
    assert m.interaction == "batch"
    assert m.side_effects == "none"  # 纯计算
    assert [mat.path for mat in m.materials if mat.required] == ["model.fmu", "sim.json"]
    assert m.credential_kinds == ()  # 本地子进程，无凭据
    assert m.licenses == ()
    assert m.prompt_pack == "fmu"


def test_fmu_satisfies_runner_plugin_protocol_and_registry_dispatch(tmp_path):
    assert isinstance(FMURunner(), contract.RunnerPlugin)
    assert "fmu" in registry.known_backends()
    plugin = registry.resolve_backend({"backend": "fmu"}, workdir=str(tmp_path))
    assert isinstance(plugin, FMURunner)
    assert plugin.manifest is FMU_MANIFEST


def test_experiment_params_accepts_fmu_backend():
    assert ExperimentParams(backend="fmu").backend == "fmu"
    with pytest.raises(ValidationError, match="unknown runner backend"):
        ExperimentParams(backend="fmi")  # 拼错仍然拒绝


# ---- 可用性守卫 ----


async def test_validate_reports_missing_fmpy_with_install_hint(monkeypatch, tmp_path):
    """没装 fmpy：后端仍注册，validate 给安装指引（可诊断，而非 unknown backend）。"""
    monkeypatch.setattr(fmu, "fmpy_available", lambda: False)
    issues = await FMURunner(workdir=make_workdir(tmp_path)).validate({})
    assert [i.code for i in issues] == ["backend.unavailable"]
    assert "pip install fmpy" in issues[0].message


# ---- validate：物料与 spec 边界 ----


async def test_validate_requires_workdir_and_materials(stub_fmpy, tmp_path):
    issues = await FMURunner().validate({})
    assert [i.code for i in issues] == ["workdir.missing"]

    workdir = tmp_path / "empty"
    workdir.mkdir()
    issues = await FMURunner().validate({"workdir": str(workdir)})  # plan.workdir 也认
    assert {(i.code, i.path) for i in issues} == {
        ("materials.missing", "model.fmu"),
        ("materials.missing", "sim.json"),
    }


async def test_validate_rejects_bad_json_and_spec_shapes(stub_fmpy, tmp_path):
    plugin = FMURunner(workdir=make_workdir(tmp_path, spec="{not json"))
    issues = await plugin.validate({})
    assert [i.code for i in issues] == ["spec.invalid"]

    bad = {"step_size": -1, "output_variables": [], "parameters": {"e": []}, "typo_key": 1}
    plugin = FMURunner(workdir=make_workdir(tmp_path / "b", spec=bad))
    issues = await plugin.validate({})
    codes = [i.code for i in issues]
    assert codes.count("spec.invalid") == 4  # stop_time 缺失/step_size/outputs/parameters
    assert ("spec.unknown_key", "warning") in [(i.code, i.severity) for i in issues]
    # spec 形状不合格就不硬闯变量表比对
    assert "spec.unknown_variable" not in codes


async def test_validate_prefers_plan_files_sim_json(stub_fmpy, tmp_path):
    """修复循环改的是 plan["files"]，validate 必须看新内容而非工作区旧盘。"""
    plugin = FMURunner(workdir=make_workdir(tmp_path))  # 盘上是合格 spec
    issues = await plugin.validate({"files": {"sim.json": '{"stop_time": -5}'}})
    assert issues and all(i.code == "spec.invalid" for i in issues)


async def test_validate_checks_variables_against_model_table(stub_fmpy, tmp_path):
    spec = {**GOOD_SPEC, "output_variables": ["h", "ghost"], "parameters": {"nope": 1}}
    plugin = FMURunner(workdir=make_workdir(tmp_path, spec=spec))
    issues = await plugin.validate({})
    assert [i.code for i in issues] == ["spec.unknown_variable", "spec.unknown_variable"]
    assert "ghost" in issues[0].message
    assert "nope" in issues[1].message


async def test_validate_rejects_unsupported_fmi_version(monkeypatch, tmp_path):
    monkeypatch.setattr(fmu, "fmpy_available", lambda: True)
    monkeypatch.setattr(
        fmu,
        "_read_model_description",
        lambda path: SimpleNamespace(fmiVersion="1.0", modelVariables=FAKE_MD.modelVariables),
    )
    issues = await FMURunner(workdir=make_workdir(tmp_path)).validate({})
    assert [i.code for i in issues] == ["fmu.unsupported_version"]
    assert "1.0" in issues[0].message


async def test_validate_surfaces_unreadable_fmu(monkeypatch, tmp_path):
    monkeypatch.setattr(fmu, "fmpy_available", lambda: True)

    def boom(path):
        raise ValueError("not a zip")

    monkeypatch.setattr(fmu, "_read_model_description", boom)
    issues = await FMURunner(workdir=make_workdir(tmp_path)).validate({})
    assert [i.code for i in issues] == ["fmu.invalid"]
    assert "not a zip" in issues[0].message


async def test_good_spec_passes_validate(stub_fmpy, tmp_path):
    assert await FMURunner(workdir=make_workdir(tmp_path)).validate({}) == []


# ---- prepare：落物料 + 路径白名单 ----


async def test_prepare_writes_files_and_rejects_escape(tmp_path):
    plugin = FMURunner(workdir=tmp_path / "wd")
    await plugin.prepare(contract.RunContext(files={"sim.json": json.dumps(GOOD_SPEC)}))
    assert json.loads((tmp_path / "wd" / "sim.json").read_text()) == GOOD_SPEC
    with pytest.raises(contract.RunnerError, match="越界"):
        await plugin.prepare(contract.RunContext(files={"../evil.txt": "x"}))
    with pytest.raises(contract.RunnerError, match="越界"):
        await plugin.prepare(contract.RunContext(files={"/abs.txt": "x"}))


async def test_unbound_plugin_refuses_lifecycle(tmp_path):
    with pytest.raises(contract.RunnerError, match="工作区"):
        await FMURunner().prepare(contract.RunContext())


# ---- poll / cancel 语义（status.json 为准，进程存活兜底） ----


async def test_poll_reads_terminal_status_json(tmp_path):
    workdir = make_workdir(tmp_path)
    plugin = FMURunner(workdir=workdir)
    (workdir / "status.json").write_text(json.dumps({"state": "succeeded"}))
    assert await plugin.poll("999999999") == contract.RunStatus(state="succeeded", exit_code=0)
    (workdir / "status.json").write_text(json.dumps({"state": "failed", "error": "求解发散"}))
    status = await plugin.poll("999999999")
    assert status.state == "failed" and status.exit_code == 1
    assert "求解发散" in status.detail


async def test_poll_flags_vanished_worker_without_status(tmp_path):
    workdir = make_workdir(tmp_path)
    status = await FMURunner(workdir=workdir).poll("999999999")  # 不存在的 pid
    assert status.state == "failed"
    assert "status.json" in status.detail


async def test_poll_reports_running_for_live_foreign_pid(tmp_path):
    workdir = make_workdir(tmp_path)
    (workdir / "status.json").write_text(json.dumps({"state": "running"}))
    status = await FMURunner(workdir=workdir).poll(str(os.getpid()))  # 本进程必然存活
    assert status.state == "running"


async def test_poll_and_cancel_reject_foreign_handle(tmp_path):
    plugin = FMURunner(workdir=make_workdir(tmp_path))
    with pytest.raises(contract.RunnerError, match="pid"):
        await plugin.poll("lease:abc")
    with pytest.raises(contract.RunnerError, match="pid"):
        await plugin.cancel("lease:abc")


async def test_cancel_kills_process_group_and_is_idempotent(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    plugin = FMURunner(workdir=tmp_path)
    try:
        await plugin.cancel(str(proc.pid))
        assert proc.wait(timeout=10) == -signal.SIGTERM
    finally:
        if proc.poll() is None:
            proc.kill()
    await plugin.cancel(str(proc.pid))  # 已结束：no-op 不抛


# ---- collect：result.csv 解析（NaN 计数不丢） ----

RESULT_CSV = (
    "time,h,v\n"
    "0.0,1.0,0.0\n"
    "0.1,0.95,-0.98\n"
    "0.2,nan,-1.96\n"  # 模型发散点：值进不了 JSONB，但计数必须保留
    "0.3,0.65,inf\n"
)


async def test_collect_parses_result_csv_keeping_nan_flags(tmp_path):
    workdir = make_workdir(tmp_path)
    (workdir / "result.csv").write_text(RESULT_CSV, encoding="utf-8")
    (workdir / "status.json").write_text(json.dumps({"state": "succeeded"}))
    bundle = await FMURunner(workdir=workdir).collect(contract.RunContext())

    series: dict[str, list] = {}
    for p in bundle.metrics:
        series.setdefault(p["name"], []).append((p["step"], p["value"]))
    # 时间列单发一条同 step 序列（step 是 int 承重面，浮点时刻靠它对齐还原）
    assert series["time"] == [(0, 0.0), (1, 0.1), (2, 0.2), (3, 0.3)]
    assert series["h"] == [(0, 1.0), (1, 0.95), (3, 0.65)]  # step=2 是 NaN，点位跳过
    assert series["v"] == [(0, 0.0), (1, -0.98), (2, -1.96)]  # step=3 是 inf，同理

    assert [f.name for f in bundle.files] == ["result.csv"]
    f = bundle.files[0]
    assert f.parser == "csv"
    assert f.summary == {
        "rows": 4,
        "columns": ["time", "h", "v"],
        "nan_counts": {"h": 1, "v": 1},  # NaN flag 不丢：计数进 summary
    }
    assert "h×1" in bundle.notes and "v×1" in bundle.notes


async def test_collect_surfaces_failure_error_even_without_csv(tmp_path):
    workdir = make_workdir(tmp_path)
    (workdir / "status.json").write_text(
        json.dumps({"state": "failed", "error": "TypeError: bad model"}), encoding="utf-8"
    )
    bundle = await FMURunner(workdir=workdir).collect(contract.RunContext())
    assert bundle.metrics == [] and bundle.files == []
    assert "bad model" in bundle.notes


# ---- worker 子进程（不需要 fmpy 的失败路径） ----


def _app_pythonpath() -> dict:
    """让子进程 `python -m app.…` 找得到 app 包（容器内 pip 装过则本来就行，
    host 直跑 pytest 时靠 PYTHONPATH 兜底）。"""
    env = dict(os.environ)
    backend_root = str(Path(__file__).parent.parent)
    env["PYTHONPATH"] = backend_root + os.pathsep + env.get("PYTHONPATH", "")
    return env


def test_worker_writes_failed_status_on_bad_spec(tmp_path):
    workdir = make_workdir(tmp_path, spec="{not json")
    proc = subprocess.run(
        [sys.executable, "-m", "app.services.runners.fmu_worker", str(workdir)],
        env=_app_pythonpath(),
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 1
    status = json.loads((workdir / "status.json").read_text())
    assert status["state"] == "failed"
    assert "sim.json" in status["error"]


async def test_launch_poll_collect_over_failing_worker(tmp_path, monkeypatch):
    """经插件真起子进程（坏 spec 快速失败）：launch→poll 至 failed→collect 有 error。"""
    workdir = make_workdir(tmp_path, spec="{not json")
    monkeypatch.setenv("PYTHONPATH", _app_pythonpath()["PYTHONPATH"])
    plugin = FMURunner(workdir=workdir)
    ctx = contract.RunContext()
    handle = await plugin.launch(ctx)
    assert handle.isdigit()
    assert ctx.scratch["launch_command"].endswith(f"fmu_worker {workdir}")
    status = await _poll_until_terminal(plugin, handle)
    assert status.state == "failed"
    assert "sim.json" in status.detail
    bundle = await plugin.collect(ctx)
    assert "sim.json" in bundle.notes
    await plugin.cleanup(ctx)


async def _poll_until_terminal(plugin, handle, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await plugin.poll(handle)
        if status.state != "running":
            return status
        await asyncio.sleep(0.2)
    raise AssertionError("worker 未在时限内结束")


# ---- 端到端：真 fmpy + Reference-FMUs BouncingBall（环境缺料则 skip） ----


async def test_end_to_end_bouncing_ball(tmp_path, monkeypatch):
    fmpy = pytest.importorskip("fmpy", reason="可选依赖 fmpy 未安装（simulation extra）")
    if not BOUNCING_BALL.is_file():
        pytest.skip("缺 tests/fixtures/BouncingBall.fmu（下载受网络限制时允许缺席）")

    workdir = tmp_path / "e2e"
    workdir.mkdir()
    fmu_copy = workdir / "model.fmu"
    fmu_copy.write_bytes(BOUNCING_BALL.read_bytes())

    # 预检：Reference-FMUs 只带 x86_64 二进制（fmpy.platform 不看 arch，光比对
    # binaries 目录名在 arm64 上会假阳）。真载入跑 0.01s 才是可信探针；载不动就
    # 用包内 sources/ 现场编译（编译进 workdir 副本，夹具不动），编译器也没有则
    # skip——本用例测的是我们的 worker 管线，不是 fmpy 在异构平台的可用性。
    try:
        fmpy.simulate_fmu(str(fmu_copy), stop_time=0.01)
    except Exception:
        try:
            from fmpy.util import compile_platform_binary

            compile_platform_binary(str(fmu_copy))
            fmpy.simulate_fmu(str(fmu_copy), stop_time=0.01)
        except Exception as e:
            pytest.skip(f"本环境无法执行 BouncingBall.fmu：{e}")
    monkeypatch.setenv("PYTHONPATH", _app_pythonpath()["PYTHONPATH"])

    plugin = registry.resolve_backend({"backend": "fmu"}, workdir=str(workdir))
    ctx = contract.RunContext(files={"sim.json": json.dumps(GOOD_SPEC)})
    assert await plugin.validate({"files": ctx.files, "workdir": str(workdir)}) == []
    await plugin.prepare(ctx)
    handle = await plugin.launch(ctx)
    status = await _poll_until_terminal(plugin, handle, timeout=120.0)
    assert status == contract.RunStatus(state="succeeded", exit_code=0)

    bundle = await plugin.collect(ctx)
    names = {p["name"] for p in bundle.metrics}
    assert {"time", "h", "v"} <= names
    heights = [p["value"] for p in bundle.metrics if p["name"] == "h"]
    assert heights and max(heights) <= 1.0 + 1e-6  # 初始高度 1m，球不会越弹越高
    assert [f.name for f in bundle.files] == ["result.csv"]
    assert bundle.files[0].summary["rows"] > 0

    await plugin.cancel(handle)  # 已结束：幂等 no-op
    await plugin.cleanup(ctx)
    with pytest.raises(NotImplementedError):
        await plugin.dry_run(ctx)
