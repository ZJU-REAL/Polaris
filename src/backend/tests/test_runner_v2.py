"""Runner v2 契约测试（#675）：manifest / 注册表分派 / python-ml 适配器映射，
外加一条经 v2 契约驱动的最小假 run（fake SSH 底座，照现有 runner 测试模式）。

动作层（actions_experiment）本阶段仍走旧路——这里只证明：经 resolve_backend
拿到的插件能独立驱动一次完整生命周期，且全部委托存量原语（行为零变化）。
"""

import json
import uuid

import pytest
import pytest_asyncio
from pydantic import ValidationError

from app.core.db import get_sessionmaker
from app.models.project import Project
from app.models.user import User
from app.schemas.experiment import ExperimentParams
from app.services import ssh_exec
from app.services.runners import contract, registry
from app.services.runners.python_ml import PYTHON_ML_MANIFEST, PythonMLRunner
from tests.fake_ssh import FakeSSHServer, FakeSSHSession

# 合格物料：满足 python-ml manifest（requirements.txt + 支持 --smoke 的 run.sh）
GOOD_FILES = {
    "requirements.txt": "numpy\n",
    "run.sh": "#!/bin/bash\npython train.py $1\n# supports --smoke\n",
    "train.py": "print('POLARIS_METRIC ' + '{}')\n",
}


# ---- manifest：物料契约降格为后端自述 ----


def test_python_ml_manifest_declares_the_legacy_contract():
    m = PYTHON_ML_MANIFEST
    assert m.backend == "python-ml"
    assert m.interaction == "batch"
    assert m.side_effects == "filesystem"
    required = [mat.path for mat in m.materials if mat.required]
    assert required == ["requirements.txt", "run.sh"]  # 原全局校验降格至此
    assert m.credential_kinds == ("ssh",)
    assert m.licenses == ()  # python-ml 无 License 席位需求
    assert m.prompt_pack == "python-ml"


def test_python_ml_satisfies_runner_plugin_protocol():
    assert isinstance(PythonMLRunner(), contract.RunnerPlugin)


# ---- 注册表与分派 ----


def test_registry_knows_python_ml_and_dispatches_default():
    assert "python-ml" in registry.known_backends()
    plugin = registry.resolve_backend({})  # plan 未声明 backend → 默认
    assert isinstance(plugin, PythonMLRunner)
    assert registry.resolve_backend({"backend": "python-ml"}).manifest is PYTHON_ML_MANIFEST
    assert registry.resolve_backend(None).manifest.backend == "python-ml"  # 非 dict 也走默认


def test_registry_rejects_unknown_backend():
    with pytest.raises(registry.UnknownBackendError) as exc:
        registry.resolve_backend({"backend": "quantum-annealer"})
    assert "quantum-annealer" in str(exc.value)
    assert "python-ml" in str(exc.value)  # 报错带出已知后端，拼错时可自纠


def test_registry_rejects_duplicate_and_empty_registration():
    with pytest.raises(ValueError, match="already registered"):
        registry.register("python-ml", PythonMLRunner.create)
    with pytest.raises(ValueError, match="non-empty"):
        registry.register("  ", PythonMLRunner.create)


# ---- schema：params.backend 校验 ∈ 注册表 ----


def test_experiment_params_backend_defaults_and_validates():
    assert ExperimentParams().backend == "python-ml"
    assert ExperimentParams(backend="python-ml").backend == "python-ml"
    with pytest.raises(ValidationError, match="unknown runner backend"):
        ExperimentParams(backend="quantum-annealer")


# ---- validate：manifest 驱动的物料静态检查（委托存量 validate_files 深检查） ----


async def test_validate_reports_all_missing_materials():
    issues = await PythonMLRunner().validate({"files": {"train.py": "x = 1\n"}})
    assert [i.path for i in issues] == ["requirements.txt", "run.sh"]
    assert all(i.code == "materials.missing" and i.severity == "error" for i in issues)
    # 完全没给物料：同样按 manifest 报齐必需项（validate 无底座也可用）
    issues = await PythonMLRunner().validate({})
    assert [i.path for i in issues] == ["requirements.txt", "run.sh"]


async def test_validate_delegates_legacy_smoke_and_syntax_checks():
    plugin = PythonMLRunner()
    no_smoke = {**GOOD_FILES, "run.sh": "#!/bin/bash\npython train.py\n"}
    issues = await plugin.validate({"files": no_smoke})
    assert [i.code for i in issues] == ["materials.invalid"]
    assert "--smoke" in issues[0].message
    bad_py = {**GOOD_FILES, "train.py": "def broken(:\n"}
    issues = await plugin.validate({"files": bad_py})
    assert [i.code for i in issues] == ["materials.invalid"]
    assert (await plugin.validate({"files": GOOD_FILES})) == []


# ---- python-ml 适配器：v2 生命周期端到端驱动一个最小假 run ----


@pytest_asyncio.fixture
async def project_id(app):
    """真实课题行：底座每条命令过 _audit 落 Activity（FK 指向 projects）。"""
    owner = User(
        id=uuid.uuid4(),
        email="runner-v2@example.com",
        hashed_password="test",
        is_active=True,
        is_superuser=False,
        is_verified=True,
    )
    project = Project(
        id=uuid.uuid4(),
        name="runner-v2",
        slug=f"runner-v2-{uuid.uuid4().hex[:8]}",
        owner_id=owner.id,
    )
    async with get_sessionmaker()() as session:
        session.add(owner)
        await session.flush()
        session.add(project)
        await session.commit()
    return project.id


def _executor(server: FakeSSHServer, project_id: uuid.UUID) -> ssh_exec.SSHExecutor:
    return ssh_exec.SSHExecutor(
        FakeSSHSession(server),
        exp_id=str(uuid.uuid4()),
        host="gpu.example",
        project_id=project_id,
    )


async def test_v2_drives_a_minimal_python_ml_run(project_id):
    """经 v2 契约走完 validate→prepare→launch→poll→collect→cancel→cleanup 全周期。

    每步都应落在存量原语上（断言 fake 收到的命令/文件与 v1 直调完全一致）。
    app 夹具提供 DB：底座每条命令过 _audit 落 Activity（v1 审计行为原样保留）。
    """
    server = FakeSSHServer(
        run_log='POLARIS_METRIC {"name": "accuracy", "step": 1, "value": 0.75}\ndone\n',
        run_exit=None,  # 先模拟仍在运行
        metrics_json=json.dumps({"f1": 0.5}),
    )
    executor = _executor(server, project_id)
    plugin = registry.resolve_backend({}, runner=executor)

    assert await plugin.validate({"files": GOOD_FILES}) == []

    ctx = contract.RunContext(plan={}, files=GOOD_FILES, substrate=executor)
    await plugin.prepare(ctx)
    sftp_root = f"polaris_runs/{executor.exp_id}"
    assert f"{sftp_root}/requirements.txt" in server.files  # 物料经 SFTP 落盘，不进 shell
    assert any(c.startswith("mkdir -p") for c in server.commands)
    assert any("pip install" in c for c in server.commands)  # setup_venv 原样执行

    handle = await plugin.launch(ctx)
    assert handle == str(server.pid)  # int pid → str 句柄（v2 唯一的形态变化）
    assert ctx.scratch["launch_command"].endswith("echo $!")  # 启动命令留档供审计

    status = await plugin.poll(handle)
    assert status.state == "running"
    server.run_exit = 0  # 假进程结束
    status = await plugin.poll(handle)
    assert status == contract.RunStatus(state="succeeded", exit_code=0)

    server.files[f"{sftp_root}/figures/primary_metric.png"] = b"\x89PNG fake"
    bundle = await plugin.collect(ctx)
    by_name = {p["name"]: p["value"] for p in bundle.metrics}
    assert by_name == {"accuracy": 0.75, "f1": 0.5}  # 双通道：日志行 + metrics.json
    assert [f.path for f in bundle.files] == ["figures/primary_metric.png"]
    assert "done" in bundle.notes

    await plugin.cancel(handle)
    assert server.killed == [server.pid]  # cancel → kill_pid（幂等，已结束也是 no-op）
    await plugin.cleanup(ctx)

    with pytest.raises(NotImplementedError):  # v1 无预演原语，dry_run 暂缓（§13）
        await plugin.dry_run(ctx)


async def test_prepare_failure_surfaces_as_runner_error(project_id):
    server = FakeSSHServer(venv_exit=1)
    plugin = PythonMLRunner(runner=_executor(server, project_id))
    with pytest.raises(contract.RunnerError, match="依赖安装失败"):
        await plugin.prepare(contract.RunContext(files=GOOD_FILES))


async def test_poll_flags_vanished_process_without_exit_file(project_id):
    """进程消失且未落盘 run.exit → failed 带说明（而非永远 running）。"""
    server = FakeSSHServer(run_exit=None)
    plugin = PythonMLRunner(runner=_executor(server, project_id))
    handle = await plugin.launch(contract.RunContext())
    server.launched = False  # 模拟进程凭空消失（kill -9 / 主机重启）
    status = await plugin.poll(handle)
    assert status.state == "failed"
    assert status.exit_code is None
    assert "run.exit" in status.detail


async def test_unbound_plugin_refuses_lifecycle_but_allows_validate():
    """resolve_backend 不传底座：validate 可用（纯静态），生命周期方法明确报错。"""
    plugin = registry.resolve_backend({})
    assert await plugin.validate({"files": GOOD_FILES}) == []
    with pytest.raises(contract.RunnerError, match="执行底座"):
        await plugin.prepare(contract.RunContext())


async def test_poll_rejects_foreign_handle(project_id):
    plugin = PythonMLRunner(runner=_executor(FakeSSHServer(), project_id))
    with pytest.raises(contract.RunnerError, match="pid"):
        await plugin.poll("lease:abc123")  # 别的后端的句柄形态
