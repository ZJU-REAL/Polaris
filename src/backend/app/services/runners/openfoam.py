"""OpenFOAM 后端：零 license CFD 容器（#681，R4；与 ngspice 同属第一档验证后端）。

它验证的是 v2 物料契约的**目录形态**：不是单文件 netlist，而是一整个 case
目录（``system/`` 数值格式与求解控制、``constant/`` 物性与网格、``0/`` 初始
边界条件）——LLM 产出的仍然只是**文件内容**，目录结构由相对路径表达。

求解器选择是「plan 参数但必须过白名单」的范本：plan.solver 只能命中
``ALLOWED_SOLVERS`` 里的键，容器里执行的命令取自按白名单**预生成**的模板
字典——plan 挑键，不拼串；白名单外直接拒绝（validate 报 error、launch 抛
RunnerError 双保险）。起步收 icoFoam/simpleFoam/pimpleFoam 三个最常用的
不可压求解器，扩清单 = 改这一个元组。

结果双通道：
- 求解器日志的残差行（``Solving for p … Final residual = 1e-06``）→
  按迭代序的残差指标（NaN 带 flag 记录：残差 nan 是发散的直接证据）；
- ``postProcessing/`` 下功能对象输出的 dat/csv 数值表 → 指标序列 + 结果文件。

镜像默认 opencfd 官方公共镜像（体积大：CI 只跑 fixture 单测，真容器冒烟
仅本地手动跑，见 tests 的 docker_integration 标记）。
"""

from __future__ import annotations

import re
from typing import Any

from app.services.runners.container_substrate import (
    ContainerBackendBase,
    ContainerSubstrate,
    metric_point,
    parse_float_table,
)
from app.services.runners.contract import (
    Issue,
    MaterialSpec,
    ResourceNeeds,
    ResultBundle,
    ResultFile,
    RunContext,
    RunHandle,
    RunnerError,
    RunnerManifest,
)

OPENFOAM_BACKEND = "openfoam"

SOLVER_LOG = "solver.log"
POSTPROCESSING_DIR = "postProcessing"

# 求解器白名单：plan.solver 的唯一合法取值域（起步三件套，扩充改这里）
ALLOWED_SOLVERS = ("icoFoam", "simpleFoam", "pimpleFoam")

# 默认镜像：OpenCFD 官方公共镜像（自带全部标准求解器；入口脚本负责 source
# OpenFOAM 环境后再执行我们的命令）。plan.image 可覆盖（白名单正则校验）。
DEFAULT_IMAGE = "opencfd/openfoam-default:latest"

# 按白名单预生成的固定模板命令：plan 只能挑键，永远拼不进串（铁律范本）。
# 前置 blockMesh：OpenFOAM 求解器要求 constant/polyMesh 网格在场，标准工作流是
# 由 system/blockMeshDict 生成——物料里带了 dict 就先建网格（失败即整体失败，
# && 短路），没带就默认网格已随物料给出。整段仍是常量模板，无任何插值来自 plan。
LAUNCH_TEMPLATES = {
    solver: (
        "if [ -f system/blockMeshDict ]; then blockMesh -case /work > blockMesh.log 2>&1"
        f" || exit $?; fi && {solver} -case /work > {SOLVER_LOG} 2>&1"
    )
    for solver in ALLOWED_SOLVERS
}

# 残差行（OpenFOAM 求解器日志逐迭代打印）；residual 交 float() 判定，nan 合法
_RESIDUAL_RE = re.compile(r"Solving for (\w+).*?Final residual = ([-+\w.]+)")

_MAX_SERIES_POINTS = 2000  # 与 ngspice 同款抽稀上限（长算例的残差/序列行数很大）

OPENFOAM_MANIFEST = RunnerManifest(
    backend=OPENFOAM_BACKEND,
    interaction="batch",
    side_effects="filesystem",  # --network none + 只挂工作目录
    materials=(
        # case 目录的三个必需 system 文件；0/ 与 constant/ 是目录约定，
        # MaterialSpec 只表达文件，目录结构检查在 validate 里补
        MaterialSpec("system/controlDict", required=True, description="求解控制（时间步/输出）"),
        MaterialSpec("system/fvSchemes", required=True, description="离散格式"),
        MaterialSpec("system/fvSolution", required=True, description="线性求解器与松弛"),
    ),
    io_schema={
        "inputs": {
            "case 目录": "system/ + constant/ + 0/ 的完整 OpenFOAM case（LLM 产文件内容）",
            "plan.solver": f"求解器名，必须 ∈ {list(ALLOWED_SOLVERS)}",
            "plan.image": f"可选容器镜像（白名单校验；默认 {DEFAULT_IMAGE}）",
        },
        "outputs": {
            SOLVER_LOG: "求解器日志（残差指标在此解析）",
            f"{POSTPROCESSING_DIR}/": "功能对象输出的 dat/csv → 指标序列 + 结果文件",
        },
    },
    # CFD 单机保守声明；#680 租约接线见 ContainerBackendBase.prepare 的 TODO
    resources=ResourceNeeds(cpu=4, mem_gb=8.0, walltime_min=240),
    licenses=(),
    credential_kinds=(),  # 本机 docker，无需连接凭据
    prompt_pack=OPENFOAM_BACKEND,
)


class OpenFOAMRunner(ContainerBackendBase):
    """OpenFOAM 批处理容器后端（生命周期公共部分见 ContainerBackendBase）。"""

    manifest = OPENFOAM_MANIFEST

    async def validate(self, plan: dict[str, Any]) -> list[Issue]:
        """case 目录结构 + 求解器白名单的静态检查。"""
        files = plan.get("files") if isinstance(plan, dict) else None
        files = files if isinstance(files, dict) else {}
        issues = [
            Issue(
                code="materials.missing",
                message=f"缺少必需物料 {m.path}（{m.description}）",
                path=m.path,
            )
            for m in self.manifest.materials
            if m.required and m.path not in files
        ]
        # 目录约定：0/ 至少一个初始场、constant/ 至少一个文件（物性或网格描述）
        for prefix, why in (
            ("0/", "初始/边界条件场（如 0/p、0/U）"),
            ("constant/", "物性/网格（如 constant/transportProperties）"),
        ):
            if not any(name.startswith(prefix) for name in files):
                issues.append(
                    Issue(
                        code="materials.missing",
                        message=f"case 缺少 {prefix} 下的文件（{why}）",
                        path=prefix,
                    )
                )
        solver = str(plan.get("solver") or "").strip() if isinstance(plan, dict) else ""
        if solver not in ALLOWED_SOLVERS:
            issues.append(
                Issue(
                    code="plan.solver_not_allowed",
                    message=(
                        f"plan.solver={solver!r} 不在白名单 {list(ALLOWED_SOLVERS)} 内"
                        "（求解器名会进容器命令行，只认白名单）"
                    ),
                )
            )
        return issues

    async def launch(self, ctx: RunContext) -> RunHandle:
        """按白名单模板字典取命令拉起容器（launch 再验一遍白名单：双保险）。"""
        substrate = self._require_substrate()
        solver = str(ctx.plan.get("solver") or "").strip()
        command = LAUNCH_TEMPLATES.get(solver)
        if command is None:
            raise RunnerError(
                f"求解器 {solver!r} 不在白名单 {list(ALLOWED_SOLVERS)} 内，拒绝启动"
            )
        image = str(ctx.plan.get("image") or "").strip() or DEFAULT_IMAGE
        handle = await substrate.launch(image=image, command=command)
        ctx.scratch["container"] = handle
        ctx.scratch["launch_command"] = command  # 审计留档
        return handle

    async def collect(self, ctx: RunContext) -> ResultBundle:
        """收结果：残差序列 + postProcessing 数值表 + 结果文件登记。"""
        substrate = self._require_substrate()
        metrics: list[dict[str, Any]] = []
        files: list[ResultFile] = []

        log_text = substrate.read_text(SOLVER_LOG)
        if log_text is not None:
            files.append(ResultFile(name=SOLVER_LOG, path=SOLVER_LOG))
            metrics.extend(self._residual_points(log_text))
        if substrate.read_text("blockMesh.log") is not None:  # 建网格日志也登记（诊断用）
            files.append(ResultFile(name="blockMesh.log", path="blockMesh.log"))

        # postProcessing/：功能对象各自建目录（<func>/<time>/<file>.dat），
        # 确定性扫描 dat/csv；指标名冠功能对象目录名，同名文件不同时间段不串
        pp_root = substrate.workdir / POSTPROCESSING_DIR
        if pp_root.is_dir():
            for path in sorted(pp_root.rglob("*")):
                if not (path.is_file() and path.suffix in (".dat", ".csv")):
                    continue
                rel = path.relative_to(substrate.workdir).as_posix()
                text = substrate.read_text(rel) or ""
                header, rows = parse_float_table(text)
                func = rel.split("/")[1] if len(rel.split("/")) > 2 else path.stem
                metrics.extend(self._table_points(func, header, rows))
                files.append(
                    ResultFile(
                        name=rel,
                        path=rel,
                        parser="table",
                        summary={"rows": len(rows), "columns": len(rows[0]) if rows else 0},
                    )
                )
        return ResultBundle(metrics=metrics, files=files, notes=substrate.tail(SOLVER_LOG))

    @staticmethod
    def _residual_points(log_text: str) -> list[dict[str, Any]]:
        """残差行 → 指标序列：每个场自己的迭代序号做 step；nan 带 flag 记录
        （残差 nan = 解发散，这正是要暴露给上层看的信号）。"""
        counters: dict[str, int] = {}
        points: list[dict[str, Any]] = []
        for m in _RESIDUAL_RE.finditer(log_text):
            field, raw = m.group(1), m.group(2)
            try:
                value = float(raw)
            except ValueError:
                continue
            step = counters.get(field, 0)
            counters[field] = step + 1
            points.append(metric_point(f"residual.{field}", step, value))
        return points

    @staticmethod
    def _table_points(
        func: str, header: list[str], rows: list[list[float]]
    ) -> list[dict[str, Any]]:
        """postProcessing 数值表 → 指标序列：首列是时间轴（OpenFOAM 惯例），
        其余列按表头命名（无表头用列号）；step 用行号（契约要求 int 序号）。"""
        if not rows:
            return []
        ncols = len(rows[0])
        names = [
            header[i] if i < len(header) else f"col{i}"
            for i in range(1, ncols)
        ]
        stride = max(1, -(-len(rows) // _MAX_SERIES_POINTS))
        points: list[dict[str, Any]] = []
        for step, row in enumerate(rows):
            if step % stride:
                continue
            for i, name in enumerate(names, start=1):
                if i < len(row):
                    points.append(metric_point(f"{func}.{name}", step, row[i]))
        return points


def create_substrate(workdir: str, **kwargs: Any) -> ContainerSubstrate:
    """便捷工厂：给动作层/冒烟测试建一个带 openfoam 前缀的底座。"""
    return ContainerSubstrate(workdir, name_prefix=f"polaris-{OPENFOAM_BACKEND}", **kwargs)
