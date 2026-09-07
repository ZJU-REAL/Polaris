"""容器后端测试（#681，R4）：ngspice / openfoam 的 manifest、validate 边界、
collect 解析器（fixture 日志/数据表，不碰 docker），加真容器冒烟（docker_integration
标记，默认 deselect；见 pyproject 的 addopts）。

铁律的测试面：docker run 的 argv 模板逐项断言（--rm/--network none/只挂工作目录/
sh -c 固定模板）、白名单外求解器与非法镜像名的拒绝、物料路径越界拒绝。
"""

import os
import shutil

import pytest
from pydantic import ValidationError

from app.schemas.experiment import ExperimentParams
from app.services.runners import container_substrate, registry
from app.services.runners.container_substrate import (
    ContainerSubstrate,
    metric_point,
    parse_float_table,
)
from app.services.runners.contract import RunContext, RunnerError, RunStatus
from app.services.runners.ngspice import NGSPICE_MANIFEST, NgspiceRunner
from app.services.runners.openfoam import (
    ALLOWED_SOLVERS,
    LAUNCH_TEMPLATES,
    OPENFOAM_MANIFEST,
    OpenFOAMRunner,
)

# ---- fixture 物料与产物 ----

RC_NETLIST = """* rc transient smoke
V1 in 0 PULSE(0 5 0 1u 1u 4m 8m)
R1 in out 1k
C1 out 0 1u
.tran 10u 5m
.control
run
meas tran vmax MAX v(out)
wrdata rc_out.dat v(out)
.endc
.end
"""

NGSPICE_LOG = """Note: No compatibility mode selected!

Circuit: * rc transient smoke

Doing analysis at TEMP = 27.000000 and TNOM = 27.000000

No. of Data Rows : 501
vmax                =  4.996717e+00 at=  2.060000e-03
idiv                =  nan
"""

# wrdata 默认布局：每个向量 (scale, value) 成对 → 1 个向量 = 2 列；含一个 NaN 值行
RC_WRDATA = """ 0.000000e+00  0.000000e+00
 1.000000e-05  4.700000e-02
 2.000000e-05  nan
 3.000000e-05  1.370000e-01
"""

OPENFOAM_LOG = """Time = 0.001

Courant Number mean: 0.1 max: 0.5
smoothSolver:  Solving for Ux, Initial residual = 1, Final residual = 8.5e-07, No Iterations 3
DICPCG:  Solving for p, Initial residual = 1, Final residual = 0.01, No Iterations 20

Time = 0.002

smoothSolver:  Solving for Ux, Initial residual = 0.1, Final residual = 2.1e-07, No Iterations 2
DICPCG:  Solving for p, Initial residual = 0.5, Final residual = nan, No Iterations 1000
"""

PROBES_DAT = """# Probe 0 (0.05 0.05 0.005)
#       Time            p
        0.001           2.5
        0.002           nan
        0.003           2.75
"""

GOOD_CASE = {
    "system/controlDict": "FoamFile {} application icoFoam;",
    "system/fvSchemes": "FoamFile {}",
    "system/fvSolution": "FoamFile {}",
    "system/blockMeshDict": "FoamFile {}",
    "0/p": "FoamFile {}",
    "0/U": "FoamFile {}",
    "constant/transportProperties": "nu 0.01;",
}


def _substrate(tmp_path, prefix="polaris-test") -> ContainerSubstrate:
    return ContainerSubstrate(tmp_path / "work", name_prefix=prefix)


# ---- manifest 与注册表分派 ----


def test_manifests_declare_batch_filesystem_zero_license():
    for m in (NGSPICE_MANIFEST, OPENFOAM_MANIFEST):
        assert m.interaction == "batch"
        assert m.side_effects == "filesystem"  # --network none + 只挂工作目录的声明面
        assert m.licenses == ()  # 零 license 正是 R4 选型标准
        assert m.credential_kinds == ()  # 本机 docker，无连接凭据
    assert [mat.path for mat in NGSPICE_MANIFEST.materials] == ["circuit.cir"]
    assert [mat.path for mat in OPENFOAM_MANIFEST.materials] == [
        "system/controlDict",
        "system/fvSchemes",
        "system/fvSolution",
    ]


def test_registry_dispatches_both_backends():
    known = registry.known_backends()
    assert "ngspice" in known and "openfoam" in known
    assert isinstance(registry.resolve_backend({"backend": "ngspice"}), NgspiceRunner)
    assert isinstance(registry.resolve_backend({"backend": "openfoam"}), OpenFOAMRunner)
    # python-ml 默认分派不受影响（存量实验行为不变）
    assert registry.resolve_backend({}).manifest.backend == "python-ml"


def test_experiment_params_accept_new_backends():
    assert ExperimentParams(backend="ngspice").backend == "ngspice"
    assert ExperimentParams(backend="openfoam").backend == "openfoam"
    with pytest.raises(ValidationError):
        ExperimentParams(backend="hfss")  # 商业 EDA 不在本期


# ---- ngspice validate 边界 ----


async def test_ngspice_validate_missing_and_empty_netlist():
    issues = await NgspiceRunner().validate({"files": {"other.txt": "x"}})
    assert [(i.code, i.path) for i in issues] == [("materials.missing", "circuit.cir")]
    issues = await NgspiceRunner().validate({"files": {"circuit.cir": "  \n\n"}})
    assert [i.code for i in issues] == ["materials.empty"]
    issues = await NgspiceRunner().validate({})  # plan 没给物料 = 缺失
    assert [i.code for i in issues] == ["materials.missing"]


async def test_ngspice_validate_title_line_and_end():
    # 首行是 .tran 指令：ngspice 会把它当标题吞掉，必须报 error
    bad = ".tran 10u 5m\nR1 in 0 1k\n.end\n"
    issues = await NgspiceRunner().validate({"files": {"circuit.cir": bad}})
    assert [i.code for i in issues] == ["materials.invalid"]
    assert ".tran" in issues[0].message
    # 缺 .end 只是 warning（不阻断，交给仿真器自己报）
    issues = await NgspiceRunner().validate({"files": {"circuit.cir": "* t\nR1 a 0 1k\n"}})
    assert [(i.code, i.severity) for i in issues] == [("materials.suspicious", "warning")]
    assert await NgspiceRunner().validate({"files": {"circuit.cir": RC_NETLIST}}) == []


# ---- ngspice collect 解析器 ----


async def test_ngspice_collect_parses_log_scalars_and_wrdata_series(tmp_path):
    substrate = _substrate(tmp_path)
    plugin = NgspiceRunner(substrate=substrate)
    ctx = RunContext(plan={}, files={"circuit.cir": RC_NETLIST}, substrate=substrate)
    await plugin.prepare(ctx)
    (substrate.workdir / "run.log").write_text(NGSPICE_LOG)
    (substrate.workdir / "rc_out.dat").write_text(RC_WRDATA)

    bundle = await plugin.collect(ctx)

    scalars = [p for p in bundle.metrics if p["name"] in ("vmax", "idiv")]
    assert scalars[0]["name"] == "vmax" and scalars[0]["step"] == 0
    assert scalars[0]["value"] == pytest.approx(4.996717)
    # NaN 带 flag 记录不丢弃（发散信号，设计报告拍板）
    assert scalars[1] == {"name": "idiv", "step": 0, "value": None, "flag": "nan"}

    series = [p for p in bundle.metrics if p["name"] == "v(out)"]
    assert [p["step"] for p in series] == [0, 1, 2, 3]
    assert series[0]["value"] == 0.0
    assert series[1]["value"] == pytest.approx(0.047)
    assert series[2] == {"name": "v(out)", "step": 2, "value": None, "flag": "nan"}
    assert series[3]["value"] == pytest.approx(0.137)

    assert [(f.name, f.parser) for f in bundle.files] == [
        ("run.log", None),
        ("rc_out.dat", "table"),
    ]
    assert bundle.files[1].summary == {"rows": 4, "columns": 2}
    assert "idiv" in bundle.notes  # notes = run.log 尾部


async def test_ngspice_collect_survives_missing_outputs(tmp_path):
    """失败的 run 也要尽量收：run.log/wrdata 都没写出来 → 空 bundle 而非异常。"""
    substrate = _substrate(tmp_path)
    plugin = NgspiceRunner(substrate=substrate)
    ctx = RunContext(files={"circuit.cir": RC_NETLIST}, substrate=substrate)
    await plugin.prepare(ctx)
    bundle = await plugin.collect(ctx)
    assert bundle.metrics == [] and bundle.files == [] and bundle.notes == ""


# ---- openfoam validate 边界与白名单 ----


async def test_openfoam_validate_good_case_with_whitelisted_solver():
    plugin = OpenFOAMRunner()
    assert await plugin.validate({"files": GOOD_CASE, "solver": "icoFoam"}) == []


async def test_openfoam_validate_reports_missing_structure():
    plugin = OpenFOAMRunner()
    files = {"system/controlDict": "x"}  # 缺 fvSchemes/fvSolution/0/constant
    issues = await plugin.validate({"files": files, "solver": "icoFoam"})
    assert [i.path for i in issues if i.code == "materials.missing"] == [
        "system/fvSchemes",
        "system/fvSolution",
        "0/",
        "constant/",
    ]


async def test_openfoam_rejects_solver_outside_whitelist():
    plugin = OpenFOAMRunner()
    issues = await plugin.validate({"files": GOOD_CASE, "solver": "rm -rf /; icoFoam"})
    assert [i.code for i in issues] == ["plan.solver_not_allowed"]
    issues = await plugin.validate({"files": GOOD_CASE})  # 不给 solver 同样拒绝
    assert [i.code for i in issues] == ["plan.solver_not_allowed"]
    # launch 双保险：白名单外直接 RunnerError，绝不进命令行
    plugin = OpenFOAMRunner(substrate=ContainerSubstrate("/nonexistent"))
    with pytest.raises(RunnerError, match="白名单"):
        await plugin.launch(RunContext(plan={"solver": "sonicFoam"}))


def test_openfoam_templates_are_fixed_strings_per_whitelist():
    """模板字典按白名单预生成：plan 只能挑键；模板本体不含任何插值痕迹。"""
    assert set(LAUNCH_TEMPLATES) == set(ALLOWED_SOLVERS)
    for solver, cmd in LAUNCH_TEMPLATES.items():
        assert f"{solver} -case /work" in cmd
        assert "blockMesh" in cmd  # 建网格前置（物料带 blockMeshDict 才执行）


# ---- openfoam collect 解析器 ----


async def test_openfoam_collect_parses_residuals_and_postprocessing(tmp_path):
    substrate = _substrate(tmp_path)
    plugin = OpenFOAMRunner(substrate=substrate)
    ctx = RunContext(plan={"solver": "icoFoam"}, files=GOOD_CASE, substrate=substrate)
    await plugin.prepare(ctx)
    (substrate.workdir / "solver.log").write_text(OPENFOAM_LOG)
    probes = substrate.workdir / "postProcessing" / "probes" / "0"
    probes.mkdir(parents=True)
    (probes / "p.dat").write_text(PROBES_DAT)

    bundle = await plugin.collect(ctx)

    ux = [p for p in bundle.metrics if p["name"] == "residual.Ux"]
    assert [(p["step"], p["value"]) for p in ux] == [(0, 8.5e-07), (1, 2.1e-07)]
    p_res = [p for p in bundle.metrics if p["name"] == "residual.p"]
    assert p_res[0] == {"name": "residual.p", "step": 0, "value": 0.01}
    # 残差 nan = 发散，flag 记录不丢弃
    assert p_res[1] == {"name": "residual.p", "step": 1, "value": None, "flag": "nan"}

    probe_p = [p for p in bundle.metrics if p["name"] == "probes.p"]
    assert [(pt["step"], pt["value"]) for pt in probe_p] == [(0, 2.5), (1, None), (2, 2.75)]
    assert probe_p[1]["flag"] == "nan"

    assert [(f.name, f.parser) for f in bundle.files] == [
        ("solver.log", None),
        ("postProcessing/probes/0/p.dat", "table"),
    ]
    assert bundle.files[1].summary == {"rows": 3, "columns": 2}
    assert "Solving for p" in bundle.notes


# ---- 底座：模板 argv、路径白名单、run.exit 终态回退 ----


async def test_docker_run_argv_is_the_fixed_template(tmp_path, monkeypatch):
    """铁律的落地断言：进 docker CLI 的 argv 逐项来自固定模板与白名单字段。"""
    calls = []

    async def fake_run_docker(*argv, timeout=None):
        calls.append(list(argv))
        return 0, "container-id\n", ""

    monkeypatch.setattr(container_substrate, "_run_docker", fake_run_docker)
    substrate = _substrate(tmp_path, prefix="polaris-ngspice")
    plugin = NgspiceRunner(substrate=substrate)
    ctx = RunContext(plan={}, files={"circuit.cir": RC_NETLIST}, substrate=substrate)
    await plugin.prepare(ctx)
    handle = await plugin.launch(ctx)

    assert handle.startswith("polaris-ngspice-")
    assert ctx.scratch["container"] == handle
    (argv,) = calls
    assert argv[:5] == ["run", "-d", "--rm", "--name", handle]
    assert argv[5:9] == ["--network", "none", "-v", f"{substrate.workdir}:/work"]
    assert argv[9:12] == ["-w", "/work", "polaris/ngspice:latest"]
    # 容器内命令 = 模块常量模板 + 底座统一追加的退出码落盘；无任何自由文本
    assert argv[12:] == ["sh", "-c", "ngspice -b circuit.cir -o run.log; echo $? > run.exit"]


async def test_substrate_rejects_bad_image_and_missing_workdir(tmp_path):
    substrate = _substrate(tmp_path)
    substrate.workdir.mkdir(parents=True)
    with pytest.raises(RunnerError, match="镜像名"):
        await substrate.launch(image="evil; rm -rf /", command="true")
    with pytest.raises(RunnerError, match="工作目录"):
        await ContainerSubstrate(tmp_path / "nope").launch(image="alpine:3.20", command="true")


def test_substrate_rejects_material_path_escape(tmp_path):
    substrate = _substrate(tmp_path)
    for bad in ("../evil.sh", "/etc/passwd", "~/x", "a/../../b"):
        with pytest.raises(RunnerError, match="越界"):
            substrate.write_materials({bad: "boom"})


async def test_poll_falls_back_to_run_exit_after_rm(tmp_path, monkeypatch):
    """--rm 收走容器后 inspect 扑空：run.exit 文件是终态唯一依据。"""

    async def inspect_gone(*argv, timeout=None):
        return 1, "", "Error: No such object"

    monkeypatch.setattr(container_substrate, "_run_docker", inspect_gone)
    substrate = _substrate(tmp_path)
    substrate.workdir.mkdir(parents=True)
    (substrate.workdir / "run.exit").write_text("0\n")
    assert await substrate.poll("gone") == RunStatus(state="succeeded", exit_code=0)
    (substrate.workdir / "run.exit").write_text("42\n")
    assert (await substrate.poll("gone")).exit_code == 42
    (substrate.workdir / "run.exit").unlink()
    status = await substrate.poll("gone")
    assert status.state == "failed" and "run.exit" in status.detail


def test_metric_point_flags_nan_and_parse_float_table():
    assert metric_point("m", 3, 1.5) == {"name": "m", "step": 3, "value": 1.5}
    assert metric_point("m", 3, float("nan")) == {
        "name": "m", "step": 3, "value": None, "flag": "nan",
    }
    header, rows = parse_float_table("# Time p\n0.1 2.5\n(0 0 0) 1\n0.2 3.5\n")
    assert header == ["Time", "p"]
    assert rows == [[0.1, 2.5], [0.2, 3.5]]  # 向量行整行跳过（本期只收标量列）


async def test_unbound_plugin_allows_validate_but_refuses_lifecycle():
    plugin = registry.resolve_backend({"backend": "ngspice"})
    assert await plugin.validate({"files": {"circuit.cir": RC_NETLIST}}) == []
    with pytest.raises(RunnerError, match="底座"):
        await plugin.prepare(RunContext())
    with pytest.raises(NotImplementedError):  # dry_run 契约占位（§13 暂缓）
        await plugin.dry_run(RunContext())


# ---- 真容器冒烟（默认 deselect；本机 docker + 镜像在场才有意义） ----

NGSPICE_SMOKE_IMAGE = os.environ.get("POLARIS_NGSPICE_IMAGE", "polaris/ngspice:local")

needs_docker = pytest.mark.skipif(shutil.which("docker") is None, reason="需要本机 docker")


@pytest.mark.docker_integration
@needs_docker
async def test_ngspice_rc_transient_smoke_end_to_end(tmp_path):
    """真跑一个 RC 电路瞬态分析到 collect（镜像小：alpine + apk add ngspice）。

    镜像默认取本地构建的 polaris/ngspice:local（POLARIS_NGSPICE_IMAGE 可覆盖）：
    printf 'FROM alpine:3.20\\nRUN apk add --no-cache ngspice\\n' \\
      | docker build -t polaris/ngspice:local -
    """
    import asyncio

    substrate = _substrate(tmp_path, prefix="polaris-ngspice")
    plugin = NgspiceRunner(substrate=substrate)
    plan = {"image": NGSPICE_SMOKE_IMAGE, "files": {"circuit.cir": RC_NETLIST}}
    assert await plugin.validate(plan) == []

    ctx = RunContext(plan=plan, files={"circuit.cir": RC_NETLIST}, substrate=substrate)
    await plugin.prepare(ctx)
    handle = await plugin.launch(ctx)
    try:
        for _ in range(120):
            status = await plugin.poll(handle)
            if status.state != "running":
                break
            await asyncio.sleep(1)
        assert status.state == "succeeded", f"仿真未成功：{status}"

        bundle = await plugin.collect(ctx)
        by_name = {p["name"]: p for p in bundle.metrics if p["step"] == 0}
        assert "vmax" in by_name, f"run.log 未解析出 vmax；notes 尾部：{bundle.notes[-500:]}"
        # 脉宽 4tau（tau=RC=1ms）：峰值 ~5*(1-e^-4)=4.91V
        assert 4.5 < by_name["vmax"]["value"] <= 5.05
        series = [p for p in bundle.metrics if p["name"] == "v(out)"]
        assert len(series) > 100  # .tran 10u/5m ≈ 500 点的序列进了指标通道
        assert {f.name for f in bundle.files} >= {"run.log", "rc_out.dat"}
    finally:
        await plugin.cleanup(ctx)


@pytest.mark.docker_integration
@needs_docker
@pytest.mark.skipif(
    not os.environ.get("POLARIS_RUN_OPENFOAM_SMOKE"),
    reason="openfoam 镜像大（GB 级），设 POLARIS_RUN_OPENFOAM_SMOKE=1 才真跑",
)
async def test_openfoam_cavity_smoke_end_to_end(tmp_path):
    """icoFoam 顶盖驱动方腔（OpenFOAM 教程最小算例）：blockMesh 建网格 → 求解 →
    collect 残差。写好但默认不跑（镜像体积原因，见 skipif）。"""
    import asyncio

    cavity = {
        "system/controlDict": (
            "FoamFile { version 2.0; format ascii; class dictionary; object controlDict; }\n"
            "application icoFoam; startFrom startTime; startTime 0; stopAt endTime;\n"
            "endTime 0.1; deltaT 0.005; writeControl timeStep; writeInterval 20;\n"
        ),
        "system/fvSchemes": (
            "FoamFile { version 2.0; format ascii; class dictionary; object fvSchemes; }\n"
            "ddtSchemes { default Euler; } gradSchemes { default Gauss linear; }\n"
            "divSchemes { default none; div(phi,U) Gauss linear; }\n"
            "laplacianSchemes { default Gauss linear orthogonal; }\n"
            "interpolationSchemes { default linear; } snGradSchemes { default orthogonal; }\n"
        ),
        "system/fvSolution": (
            "FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }\n"
            "solvers { p { solver PCG; preconditioner DIC; tolerance 1e-06; relTol 0.05; }\n"
            "  pFinal { $p; relTol 0; }\n"
            "  U { solver smoothSolver; smoother symGaussSeidel; tolerance 1e-05; relTol 0; } }\n"
            "PISO { nCorrectors 2; nNonOrthogonalCorrectors 0;\n"
            "  pRefCell 0; pRefValue 0; }\n"
        ),
        "system/blockMeshDict": (
            "FoamFile { version 2.0; format ascii; class dictionary; object blockMeshDict; }\n"
            "scale 0.1;\n"
            "vertices ( (0 0 0) (1 0 0) (1 1 0) (0 1 0)"
            " (0 0 0.1) (1 0 0.1) (1 1 0.1) (0 1 0.1) );\n"
            "blocks ( hex (0 1 2 3 4 5 6 7) (20 20 1) simpleGrading (1 1 1) );\n"
            "boundary (\n"
            "  movingWall { type wall; faces ( (3 7 6 2) ); }\n"
            "  fixedWalls { type wall; faces ( (0 4 7 3) (2 6 5 1) (1 5 4 0) ); }\n"
            "  frontAndBack { type empty; faces ( (0 3 2 1) (4 5 6 7) ); }\n"
            ");\n"
        ),
        "0/p": (
            "FoamFile { version 2.0; format ascii; class volScalarField; object p; }\n"
            "dimensions [0 2 -2 0 0 0 0]; internalField uniform 0;\n"
            "boundaryField { movingWall { type zeroGradient; }\n"
            "  fixedWalls { type zeroGradient; } frontAndBack { type empty; } }\n"
        ),
        "0/U": (
            "FoamFile { version 2.0; format ascii; class volVectorField; object U; }\n"
            "dimensions [0 1 -1 0 0 0 0]; internalField uniform (0 0 0);\n"
            "boundaryField { movingWall { type fixedValue; value uniform (1 0 0); }\n"
            "  fixedWalls { type noSlip; } frontAndBack { type empty; } }\n"
        ),
        "constant/transportProperties": (
            "FoamFile { version 2.0; format ascii; class dictionary;"
            " object transportProperties; }\n"
            "nu [0 2 -1 0 0 0 0] 0.01;\n"
        ),
    }
    substrate = _substrate(tmp_path, prefix="polaris-openfoam")
    plugin = OpenFOAMRunner(substrate=substrate)
    plan = {"solver": "icoFoam", "files": cavity}
    assert await plugin.validate(plan) == []
    ctx = RunContext(plan=plan, files=cavity, substrate=substrate)
    await plugin.prepare(ctx)
    handle = await plugin.launch(ctx)
    try:
        for _ in range(300):
            status = await plugin.poll(handle)
            if status.state != "running":
                break
            await asyncio.sleep(2)
        tail = substrate.tail("solver.log")
        assert status.state == "succeeded", f"求解未成功：{status}；日志尾：{tail}"
        bundle = await plugin.collect(ctx)
        residuals = [p for p in bundle.metrics if p["name"].startswith("residual.")]
        assert residuals, "求解器日志未解析出残差序列"
    finally:
        await plugin.cleanup(ctx)
