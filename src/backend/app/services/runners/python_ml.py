"""python-ml 后端：现有 19 原语 Runner 包成第一个 RunnerPlugin（行为零变化）。

这是 v2 契约的**存量适配器**：不重写任何执行逻辑，每个生命周期方法委托
app/agents/voyage/runner.py 的现有原语。19 原语 → v2 方法落点：

    v2 方法      委托的 v1 原语                               备注
    ---------   ------------------------------------------  ----------------------------
    validate    （无远端原语）validate_files 的物料/冒烟契约    纯静态检查，manifest 驱动
    prepare     mkdir_workdir + write_files + setup_venv     后台脱离版 launch_setup/
                                                             read_setup_exit/read_setup_log
                                                             是同一步的可恢复形态，修复循环
                                                             仍由动作层直接驱动
    launch      launch_run                                   int pid → str 句柄
    poll        read_exit_code + check_pid                   幂等只读
    collect     tail_log + read_metrics_json + list_dir      标量指标双通道 + figures 文件
                + read_file（文件内容按需另取）
    cancel      kill_pid
    cleanup     close                                        缺位：v1 只有断连原语，无
                                                             「清工作区/释放资源」——工作区
                                                             留在远端是现网有意行为（复查
                                                             产物）；License/设备释放等 R2
                                                             资源模型落地后需补新原语
    dry_run     ——                                           缺位：v1 无预演原语（run_smoke
                                                             是小样本**真实执行**，有副作用，
                                                             不是 dry run），NotImplemented

    未进 v2 生命周期的 v1 原语（留在动作层，另有归宿）：
    - run_smoke / run_plot / ensure_plot_deps：python-ml 专属的冒烟与产图**动作**，
      属于该后端的 prompt pack 流程（R3 process pack 收编），不是通用生命周期；
    - probe_gpu / read_setup_exit / read_setup_log / launch_setup：资源预检与
      可恢复安装，R2 Resource 模型与 v2 prepare 的异步句柄形态落地后吸收；
    - workdir / read_file / list_dir / tail_log / read_metrics_json / read_exit_code /
      check_pid：观测面原语，poll/collect 之外动作层（运行台/代码浏览）仍直接消费。

安全铁律不变：LLM 只产文件内容（requirements.txt/run.sh/*.py），经 SFTP 落盘；
本适配器与底层原语只跑固定模板命令（bash run.sh、python plot_figures.py 等）。
"""

from __future__ import annotations

from typing import Any

from app.agents.voyage.runner import Runner
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

PYTHON_ML_BACKEND = "python-ml"

# 今天硬编码在全局 validate_files 里的 requirements.txt + run.sh --smoke 契约，
# 降格为本后端的物料声明（设计报告 §13：物料契约按后端自述，不再全局绑死）。
PYTHON_ML_MANIFEST = RunnerManifest(
    backend=PYTHON_ML_BACKEND,
    interaction="batch",
    side_effects="filesystem",
    materials=(
        MaterialSpec(
            "requirements.txt",
            required=True,
            description="pip 依赖清单（venv/容器内增量安装）",
        ),
        MaterialSpec(
            "run.sh",
            required=True,
            description="实验入口脚本；必须支持 --smoke 小样本快速自检",
        ),
    ),
    io_schema={
        "inputs": {"files": "LLM 产出的实验代码文件（相对工作区路径 → 文本内容）"},
        "outputs": {
            "run.log": "stdout/stderr 合流日志（POLARIS_METRIC 行内嵌标量指标）",
            "run.exit": "退出码落盘文件（poll 的判定依据）",
            "metrics.json": "可选结构化指标（平台确定性解析，非 LLM）",
            "figures/": "plot_figures.py 产出的图表",
        },
    },
    # 资源需求不在 manifest 里写死：GPU 用量由 plan/资源预检按实验决定（R2 接管调度）。
    licenses=(),
    credential_kinds=("ssh",),
    prompt_pack=PYTHON_ML_BACKEND,
)


class PythonMLRunner:
    """把现有 Runner（SSH 裸机/容器）适配成 RunnerPlugin。

    实例绑定一次 run 的执行底座（底座持有 SSH 会话），不跨 run 复用。
    validate 是纯静态检查，可以在无底座时（resolve_backend 不传 runner）调用；
    其余生命周期方法要求底座在场。
    """

    manifest = PYTHON_ML_MANIFEST

    def __init__(self, runner: Runner | None = None) -> None:
        self._runner = runner

    @classmethod
    def create(cls, *, runner: Runner | None = None, **_: Any) -> PythonMLRunner:
        """注册表工厂入口（多余的 substrate 关键字忽略：别的后端可能收别的底座）。"""
        return cls(runner=runner)

    def _require_runner(self) -> Runner:
        if self._runner is None:
            raise RunnerError("python-ml 插件未绑定执行底座（resolve_backend 需传 runner=…）")
        return self._runner

    async def validate(self, plan: dict[str, Any]) -> list[Issue]:
        """物料契约静态校验：manifest 声明的必需文件 + 存量 validate_files 的深检查。

        plan["files"] 携带物料内容（相对路径 → 文本）。先按 manifest 报齐所有缺失项
        （比 validate_files 的首错即抛对修复循环更友好），再委托存量检查兜
        路径白名单 / run.sh --smoke / Python 语法（延迟导入避免 schemas→registry→
        actions_experiment→schemas 的环）。
        """
        files = plan.get("files") if isinstance(plan, dict) else None
        if not isinstance(files, dict) or not files:
            return [
                Issue(
                    code="materials.missing",
                    message=f"缺少必需物料 {m.path}（{m.description}）",
                    path=m.path,
                )
                for m in self.manifest.materials
                if m.required
            ]
        issues = [
            Issue(
                code="materials.missing",
                message=f"缺少必需物料 {m.path}（{m.description}）",
                path=m.path,
            )
            for m in self.manifest.materials
            if m.required and m.path not in files
        ]
        if issues:
            return issues
        from app.agents.voyage.actions_experiment import validate_files

        try:
            validate_files({"files": files})
        except ValueError as e:
            issues.append(Issue(code="materials.invalid", message=str(e)))
        return issues

    async def prepare(self, ctx: RunContext) -> None:
        """备环境 = mkdir_workdir + write_files + setup_venv（前台形态）。

        动作层的可恢复安装（launch_setup + read_setup_exit/read_setup_log 轮询、
        pip 失败回 LLM 修 requirements.txt）是同一步的脱离形态，仍由动作层驱动；
        v2 的 prepare 引入异步句柄形态时再吸收。
        """
        runner = self._require_runner()
        made = await runner.mkdir_workdir()
        if made.exit_status != 0:
            raise RunnerError(f"创建工作区失败：{(made.stderr or made.stdout).strip()[:300]}")
        if ctx.files:
            await runner.write_files(ctx.files)
        setup = await runner.setup_venv()
        if setup.exit_status != 0:
            raise RunnerError(f"依赖安装失败：{(setup.stderr or setup.stdout).strip()[:300]}")

    async def launch(self, ctx: RunContext) -> RunHandle:
        """启动正式运行：launch_run 的 int pid 包成 str 句柄；启动命令记入 scratch 供审计。"""
        runner = self._require_runner()
        pid, command = await runner.launch_run()
        ctx.scratch["launch_command"] = command
        return str(pid)

    async def poll(self, handle: RunHandle) -> RunStatus:
        runner = self._require_runner()
        try:
            pid = int(handle)
        except ValueError as e:  # 句柄一定来自本插件的 launch；非数字 = 用错了插件/句柄
            raise RunnerError(f"python-ml 句柄应为 pid 字符串，得到 {handle!r}") from e
        exit_code = await runner.read_exit_code()
        if exit_code is None and not await runner.check_pid(pid):
            # 竞态：进程可能在两次探测之间刚好结束——再读一次 run.exit 才能下结论
            exit_code = await runner.read_exit_code()
            if exit_code is None:
                return RunStatus(
                    state="failed",
                    detail="进程已退出但未落盘 run.exit（疑被 kill -9 或主机重启）",
                )
        if exit_code is None:
            return RunStatus(state="running")
        if exit_code == 0:
            return RunStatus(state="succeeded", exit_code=0)
        return RunStatus(state="failed", exit_code=exit_code)

    async def collect(self, ctx: RunContext) -> ResultBundle:
        """收结果：标量指标双通道（run.log 的 POLARIS_METRIC 行 + 可选 metrics.json）
        照抄动作层的确定性解析；figures/ 走 v2 新增的结果文件通道。

        与已入库指标点的去重是调用方的事（动作层轮询期间已逐段解析过日志）。
        """
        runner = self._require_runner()
        from app.agents.voyage.actions_experiment import parse_metric_lines, parse_metrics_json

        metrics: list[dict[str, Any]] = []
        log_text, _ = await runner.tail_log(0)
        metrics.extend(parse_metric_lines(log_text))
        metrics_text = await runner.read_metrics_json()
        if metrics_text:
            metrics.extend(parse_metrics_json(metrics_text))
        files = [
            ResultFile(name=name, path=f"figures/{name}")
            for name in await runner.list_dir("figures")
        ]
        notes = log_text[-2000:].strip()
        return ResultBundle(metrics=metrics, files=files, notes=notes)

    async def cancel(self, handle: RunHandle) -> None:
        runner = self._require_runner()
        try:
            pid = int(handle)
        except ValueError as e:
            raise RunnerError(f"python-ml 句柄应为 pid 字符串，得到 {handle!r}") from e
        await runner.kill_pid(pid)  # v1 语义：kill TERM，尽力而为（已退出 = no-op）

    async def cleanup(self, ctx: RunContext) -> None:
        """善后 = close（断 SSH 连接）。

        缺位原语：v1 没有「清工作区/释放资源」——远端工作区**有意**保留（用户复查
        产物、失败诊断都要它）；License/设备归位要等 R2 资源模型给出新原语。
        """
        runner = self._require_runner()
        await runner.close()

    async def dry_run(self, ctx: RunContext) -> RunStatus:
        # 缺位原语：v1 无预演能力。run_smoke 是小样本**真实执行**（装依赖、写文件、
        # 跑代码），不是无副作用的 dry run，不能拿来冒充。契约占位，暂缓启用（§13）。
        raise NotImplementedError("python-ml 后端暂无 dry_run（v1 无预演原语）")
