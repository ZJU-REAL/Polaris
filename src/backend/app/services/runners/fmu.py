"""fmu 后端：FMPy 驱动 FMI 2.0/3.0 模型产物的本地批式仿真（#682，R5）。

为什么值得单列一个后端：FMI 是 200+ 建模工具（Modelica 系、Simulink、Amesim、
Dymola…）共同的导出标准——吃下 .fmu 一种物料，等于一次接入整个工具生态的产物，
是 P3 追踪（#674）里杠杆最高的一项。

与 python-ml 的关键差异（也是本文件多数设计取舍的来源）：

- **物料是二进制**：model.fmu 是 zip 打包的二进制产物（含各平台共享库），走不了
  ``plan["files"]`` 的文本通道——它由用户上传落在本地工作区，LLM 只产出 sim.json
  （仿真规格，纯 JSON 数据）。安全铁律因此更简单：LLM 文本连命令行的边都碰不到，
  唯一入口 sim.json 经 JSON 解析后按键取值喂给 fmpy 的 API 参数。
- **执行底座是本地目录**：不是 SSH 主机。仿真在本机跑，但 fmpy.simulate_fmu 是
  同步长跑调用，绝不能在事件循环里直接调——launch 起**子进程**执行固定模板
  ``python -m app.services.runners.fmu_worker <workdir>``，可变部分只有平台自己
  管理的工作区路径；worker 读 sim.json、跑仿真、把 result.csv + status.json 落盘。
- **纯计算**：不动网络不动设备，side_effects="none"。

可用性守卫：fmpy 是可选依赖（``pip install "polaris-backend[simulation]"``）。
没装时后端**仍然注册**，validate 直接报「后端不可用 + 安装指引」——比不注册更可
诊断：不注册的话用户看到的是 "unknown runner backend 'fmu'"，分不清拼错和没装。
探测用 importlib.util.find_spec（不真 import），注册表 import 不背 numpy/lxml 的量。
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import json
import math
import os
import signal
import sys
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from app.services.runners.contract import (
    Issue,
    MaterialSpec,
    ResultBundle,
    ResultFile,
    RunContext,
    RunHandle,
    RunnerError,
    RunnerManifest,
    RunStatus,
)

FMU_BACKEND = "fmu"

# fmpy 接受的 FMI 版本（1.0 太老：变量表/因果语义差异大，主流工具早已导出 2.0+）
SUPPORTED_FMI_VERSIONS = ("2.0", "3.0")

INSTALL_HINT = "后端不可用：未安装 fmpy——pip install fmpy（或 polaris-backend[simulation]）"

FMU_MANIFEST = RunnerManifest(
    backend=FMU_BACKEND,
    interaction="batch",
    side_effects="none",  # 纯计算：读 model.fmu + sim.json，产出只落本工作区
    materials=(
        MaterialSpec(
            "model.fmu",
            required=True,
            description="FMI 2.0/3.0 模型产物（二进制 zip，用户上传，不经 LLM 文本通道）",
        ),
        MaterialSpec(
            "sim.json",
            required=True,
            description="仿真规格：{stop_time, step_size?, output_variables, parameters?}",
        ),
    ),
    io_schema={
        "inputs": {
            "model.fmu": "任意 FMI 2.0/3.0 导出工具的产物",
            "sim.json": {
                "stop_time": "仿真终止时间（秒，必填，> 0）",
                "step_size": "输出采样间隔（秒，可选，> 0；缺省由求解器决定）",
                "output_variables": "要记录的模型变量名列表（必填，须存在于模型变量表）",
                "parameters": "启动参数覆盖 {变量名: 值}（可选，须存在于模型变量表）",
            },
        },
        "outputs": {
            "result.csv": "time 列 + 每个 output_variable 一列的时间序列",
            "status.json": "worker 落盘的运行状态（poll 的判定依据）",
            "run.log": "worker stdout/stderr 合流日志",
        },
    },
    licenses=(),
    credential_kinds=(),  # 本地子进程执行，无凭据概念
    prompt_pack=FMU_BACKEND,
)


def fmpy_available() -> bool:
    """fmpy 装了没有（find_spec 只查不 import；单列成函数便于测试打桩）。"""
    return find_spec("fmpy") is not None


def _read_model_description(fmu_path: str) -> Any:
    """读 FMU 的 modelDescription（延迟 import fmpy；单列成函数便于测试打桩）。"""
    from fmpy import read_model_description

    return read_model_description(fmu_path)


def _validate_sim_spec(spec: Any) -> list[Issue]:
    """sim.json 的形状校验（纯 JSON 层面，不碰模型）。"""
    if not isinstance(spec, dict):
        return [
            Issue(code="spec.invalid", message="sim.json 顶层必须是 JSON 对象", path="sim.json")
        ]
    issues: list[Issue] = []

    stop_time = spec.get("stop_time")
    if not isinstance(stop_time, int | float) or isinstance(stop_time, bool) or stop_time <= 0:
        issues.append(
            Issue(code="spec.invalid", message="sim.json 缺少正数 stop_time", path="sim.json")
        )

    step_size = spec.get("step_size")
    if step_size is not None and (
        not isinstance(step_size, int | float) or isinstance(step_size, bool) or step_size <= 0
    ):
        issues.append(
            Issue(code="spec.invalid", message="sim.json 的 step_size 须为正数", path="sim.json")
        )

    outputs = spec.get("output_variables")
    if (
        not isinstance(outputs, list)
        or not outputs
        or not all(isinstance(v, str) and v.strip() for v in outputs)
    ):
        issues.append(
            Issue(
                code="spec.invalid",
                message="sim.json 的 output_variables 须为非空字符串列表",
                path="sim.json",
            )
        )

    parameters = spec.get("parameters")
    if parameters is not None and (
        not isinstance(parameters, dict)
        or not all(
            isinstance(k, str) and isinstance(v, int | float | bool | str)
            for k, v in parameters.items()
        )
    ):
        issues.append(
            Issue(
                code="spec.invalid",
                message="sim.json 的 parameters 须为 {变量名: 数值/布尔/字符串}",
                path="sim.json",
            )
        )

    known = {"stop_time", "step_size", "output_variables", "parameters"}
    for key in spec:
        if key not in known:  # 拼错键静默忽略会让用户以为设置生效了——报 warning
            issues.append(
                Issue(
                    code="spec.unknown_key",
                    message=f"sim.json 含未知键 {key!r}（会被忽略；已知键：{sorted(known)}）",
                    path="sim.json",
                    severity="warning",
                )
            )
    return issues


def _validate_relpath(name: str) -> str:
    """工作区内相对路径白名单（同 ssh_exec._validate_relpath 语义，本地版）。"""
    raw = str(name).strip()
    path = Path(raw)
    if not raw or path.is_absolute() or raw.startswith("~") or ".." in path.parts:
        raise RunnerError(f"文件路径越界（须为 workdir 内相对路径）：{name!r}")
    return raw


class FMURunner:
    """FMPy FMU 后端插件：本地工作区为底座，子进程 worker 为执行体。

    实例绑定一次 run 的本地工作区（factory 传 workdir=…）。validate 是静态检查，
    无绑定工作区时也可用（从 plan["workdir"] 取）；launch 之后实例还持有子进程
    对象（事件循环替我们收尸，poll 直接读 returncode，跨进程恢复时退化为信号探活）。
    """

    manifest = FMU_MANIFEST

    def __init__(self, workdir: str | os.PathLike[str] | None = None) -> None:
        self._workdir = Path(workdir) if workdir is not None else None
        self._proc: asyncio.subprocess.Process | None = None

    @classmethod
    def create(cls, *, workdir: str | os.PathLike[str] | None = None, **_: Any) -> FMURunner:
        """注册表工厂入口（多余的 substrate 关键字忽略：别的后端收别的底座）。"""
        return cls(workdir=workdir)

    def _require_workdir(self) -> Path:
        if self._workdir is None:
            raise RunnerError("fmu 插件未绑定工作区（resolve_backend 需传 workdir=…）")
        return self._workdir

    # ---- validate ----

    async def validate(self, plan: dict[str, Any]) -> list[Issue]:
        """静态校验：fmpy 可用性 → 物料在场 → sim.json 形状 → 模型变量表比对。

        比对模型变量表要解开 FMU 读 modelDescription.xml——这仍算「静态」：只读
        不执行模型二进制，量级毫秒。发现问题尽量报全（对修复循环友好），但前一层
        塌了就不硬闯下一层（sim.json 都不是 JSON 就没有变量可比）。
        """
        if not fmpy_available():
            return [Issue(code="backend.unavailable", message=INSTALL_HINT)]

        workdir = self._workdir
        if workdir is None and isinstance(plan, dict) and plan.get("workdir"):
            workdir = Path(str(plan["workdir"]))
        if workdir is None:
            return [
                Issue(
                    code="workdir.missing",
                    message="fmu 后端需要本地工作区（插件绑定 workdir 或 plan.workdir）",
                )
            ]

        issues: list[Issue] = []
        fmu_path = workdir / "model.fmu"
        if not fmu_path.is_file():
            issues.append(
                Issue(
                    code="materials.missing",
                    message="缺少必需物料 model.fmu（FMI 2.0/3.0 模型产物）",
                    path="model.fmu",
                )
            )

        # sim.json 优先取 plan["files"]（LLM 修复循环改的是它），否则读工作区已落盘的
        files = plan.get("files") if isinstance(plan, dict) else None
        sim_text: str | None = None
        if isinstance(files, dict) and isinstance(files.get("sim.json"), str):
            sim_text = files["sim.json"]
        elif (workdir / "sim.json").is_file():
            sim_text = (workdir / "sim.json").read_text(encoding="utf-8")
        if sim_text is None:
            issues.append(
                Issue(
                    code="materials.missing",
                    message="缺少必需物料 sim.json（仿真规格）",
                    path="sim.json",
                )
            )
            return issues

        try:
            spec = json.loads(sim_text)
        except json.JSONDecodeError as e:
            issues.append(
                Issue(code="spec.invalid", message=f"sim.json 不是合法 JSON：{e}", path="sim.json")
            )
            return issues
        spec_issues = _validate_sim_spec(spec)
        issues.extend(spec_issues)
        if not fmu_path.is_file() or any(i.severity == "error" for i in spec_issues):
            return issues

        try:
            md = _read_model_description(str(fmu_path))
        except Exception as e:  # fmpy 抛的异常类型不稳定（zip 坏/xml 坏各不同），统一收口
            issues.append(
                Issue(
                    code="fmu.invalid",
                    message=f"model.fmu 无法解析 modelDescription：{e}",
                    path="model.fmu",
                )
            )
            return issues

        version = str(getattr(md, "fmiVersion", "") or "")
        if version not in SUPPORTED_FMI_VERSIONS:
            issues.append(
                Issue(
                    code="fmu.unsupported_version",
                    message=(
                        f"FMI 版本 {version or '未知'} 不受支持"
                        f"（支持 {'/'.join(SUPPORTED_FMI_VERSIONS)}）"
                    ),
                    path="model.fmu",
                )
            )

        known_vars = {v.name for v in getattr(md, "modelVariables", []) or []}
        for name in spec.get("output_variables") or []:
            if name not in known_vars:
                issues.append(
                    Issue(
                        code="spec.unknown_variable",
                        message=f"output_variables 里的 {name!r} 不在模型变量表中",
                        path="sim.json",
                    )
                )
        for name in spec.get("parameters") or {}:
            if name not in known_vars:
                issues.append(
                    Issue(
                        code="spec.unknown_variable",
                        message=f"parameters 里的 {name!r} 不在模型变量表中",
                        path="sim.json",
                    )
                )
        return issues

    # ---- lifecycle ----

    async def prepare(self, ctx: RunContext) -> None:
        """备环境 = 建工作区 + 落文本物料（sim.json）。model.fmu 由上传通道先期落位。"""
        workdir = self._require_workdir()
        workdir.mkdir(parents=True, exist_ok=True)
        for name, content in (ctx.files or {}).items():
            rel = _validate_relpath(name)
            target = workdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    async def launch(self, ctx: RunContext) -> RunHandle:
        """起子进程 worker 跑仿真（固定模板命令，唯一可变部分是平台管理的工作区路径）。

        为什么必须子进程：fmpy.simulate_fmu 是同步调用，物理仿真长跑几分钟到几小时，
        放线程也压不住（模型二进制段错误会带崩整个 API 进程）；子进程既不阻塞事件循环
        又隔离崩溃。start_new_session 让 worker 自成进程组：cancel 杀整组，模型二进制
        再 fork 出的求解器子进程也一并带走。
        """
        workdir = self._require_workdir()
        workdir.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, "-m", "app.services.runners.fmu_worker", str(workdir)]
        log = open(workdir / "run.log", "ab")  # noqa: SIM115 —— fd 交给子进程，随其退出关闭
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *command,
                stdout=log,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(workdir),
                start_new_session=True,
            )
        except OSError as e:
            raise RunnerError(f"启动 fmu worker 失败：{e}") from e
        finally:
            log.close()  # 子进程已继承 fd，父进程这份立刻还掉
        ctx.scratch["launch_command"] = " ".join(command)
        return str(self._proc.pid)

    def _read_status(self) -> dict[str, Any] | None:
        """读 worker 落盘的 status.json（写端 os.replace 原子落盘，读到即完整）。"""
        path = self._require_workdir() / "status.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _pid_alive(self, pid: int) -> bool:
        """进程存活探测：本实例 launch 的直接读 returncode（事件循环已收尸，
        kill 0 在僵尸期会误报存活）；跨进程恢复的句柄退化为信号探活。"""
        if self._proc is not None and self._proc.pid == pid:
            return self._proc.returncode is None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # 进程在，只是不属于我们（几乎不会发生，保守判存活）
        return True

    async def poll(self, handle: RunHandle) -> RunStatus:
        """状态判定以 status.json 为准（worker 的终局落盘），进程存活只兜底：
        进程没了又没有终局状态 = worker 被 kill -9 / 机器重启，判 failed 带说明。"""
        try:
            pid = int(handle)
        except ValueError as e:
            raise RunnerError(f"fmu 句柄应为 pid 字符串，得到 {handle!r}") from e
        status = self._read_status()
        state = (status or {}).get("state")
        if state == "succeeded":
            return RunStatus(state="succeeded", exit_code=0)
        if state == "failed":
            return RunStatus(
                state="failed", exit_code=1, detail=str((status or {}).get("error") or "")
            )
        if self._pid_alive(pid):
            return RunStatus(state="running")
        return RunStatus(
            state="failed",
            detail="worker 进程已退出但未落盘终局 status.json（疑被 kill -9 或主机重启）",
        )

    async def collect(self, ctx: RunContext) -> ResultBundle:
        """收结果：result.csv → 每个 output_variable 一条指标序列。

        指标点沿用承重面形状 {name, step, value}（step=行号 int）。时间列另发一条
        同 step 的 "time" 序列——契约的 step 是整数，塞不下浮点时刻，而砍掉时间轴
        变步长仿真的结果就废了；同 step 对齐后 UI/下游随时能还原 (t, y)。
        NaN/Inf 进不了 JSONB（见 actions_experiment._is_storable_number 的账），
        点位跳过但**计数不丢**：nan_counts 进 result.csv 的 summary 与 notes——
        仿真算出 NaN 常是模型发散的信号，静默吞掉会把坏 run 伪装成好 run。
        """
        workdir = self._require_workdir()
        status = self._read_status() or {}
        metrics: list[dict[str, Any]] = []
        files: list[ResultFile] = []
        nan_counts: dict[str, int] = {}
        rows = 0
        columns: list[str] = []

        result_path = workdir / "result.csv"
        if result_path.is_file():
            with result_path.open(newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None) or []
                columns = list(header)
                for step, row in enumerate(reader):
                    rows += 1
                    for name, cell in zip(header, row, strict=False):
                        try:
                            value = float(cell)
                        except (TypeError, ValueError):
                            value = math.nan
                        if not math.isfinite(value):
                            nan_counts[name] = nan_counts.get(name, 0) + 1
                            continue
                        metrics.append({"name": name, "step": step, "value": value})
            files.append(
                ResultFile(
                    name="result.csv",
                    path="result.csv",
                    parser="csv",
                    summary={"rows": rows, "columns": columns, "nan_counts": nan_counts},
                )
            )

        notes: list[str] = []
        if status.get("state") == "failed" and status.get("error"):
            notes.append(f"仿真失败：{status['error']}")
        if nan_counts:
            detail = "、".join(f"{k}×{v}" for k, v in sorted(nan_counts.items()))
            notes.append(f"结果含非有限值（NaN/Inf，点位已跳过但计数保留）：{detail}")
        if rows:
            notes.append(f"result.csv 共 {rows} 行 × {len(columns)} 列")
        return ResultBundle(metrics=metrics, files=files, notes="\n".join(notes))

    async def cancel(self, handle: RunHandle) -> None:
        """取消 = SIGTERM 整个进程组（launch 时 start_new_session，组长 pid=句柄）。
        已结束/不存在 = no-op（幂等）。"""
        try:
            pid = int(handle)
        except ValueError as e:
            raise RunnerError(f"fmu 句柄应为 pid 字符串，得到 {handle!r}") from e
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # 已结束
        except (PermissionError, OSError):
            # 进程组杀不动（跨会话恢复等边角）退回杀单进程，仍尽力而为
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGTERM)

    async def cleanup(self, ctx: RunContext) -> None:
        """善后：确保子进程收尸完成。工作区**有意**保留（复查 result.csv/run.log），
        与 python-ml 保留远端工作区同一取舍；无 License/设备可释放。"""
        if self._proc is not None and self._proc.returncode is None:
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(self._proc.wait(), timeout=0.1)

    async def dry_run(self, ctx: RunContext) -> RunStatus:
        # 契约占位暂缓启用（§13）。真要预演可只走 validate 的 modelDescription 检查。
        raise NotImplementedError("fmu 后端暂无 dry_run（§13 契约占位暂缓）")
