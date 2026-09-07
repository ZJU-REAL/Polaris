"""Runner v2 契约：manifest 自述 + 七个生命周期方法（设计报告 §13）。

为什么要 v2（现有 Runner Protocol 的四个硬伤，仓库摸底实证）：
- 19 个原语里 ``setup_venv``/``probe_gpu``/``ensure_plot_deps`` 是 ML 专属，
  仿真/仪器后端根本没有 venv 或 GPU 概念；
- 分派靠「plan 有无 container」二值推断，``kind`` 参数被 noqa 忽略；
- ``launch_run`` 返回 int pid——仪器会话、集群作业号、License 租约装不下；
- 指标只有 stdout 标量序列一条通道，CSV/mat/图像等结果文件没有落点。

v2 把「后端是什么、要什么、怎么跑」收进 ``RunnerManifest`` 自述（今天全局硬编码的
requirements.txt + run.sh --smoke 物料检查，降格为 python-ml 一个后端的契约），把
执行生命周期收敛为 validate → prepare → launch → poll → collect → cancel → cleanup
七个方法，句柄一律 str。现有 19 原语不动，由 ``python_ml.PythonMLRunner`` 适配。

安全铁律（v1 延续，任何后端不得破坏）：
- **LLM 只产出文件内容**（run.sh / 求解器 case / netlist …），经 SFTP 等纯数据通道落盘；
- **平台只跑 manifest 白名单的固定模板命令**，可变部分只有实验产物文件本身；
- 求解器 CLI 参数来自 manifest 白名单（``parse_container_spec`` 的白名单校验模式），
  永不来自 LLM 自由文本拼接。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

# 运行句柄：一律字符串。python-ml 装 str(pid)；将来集群作业号 / gRPC 会话 id /
# License 租约 id 都装得下（v1 的 int pid 是它的特例）。
RunHandle = str

Interaction = Literal["batch", "session", "streaming"]
SideEffects = Literal["none", "filesystem", "network", "physical"]
Severity = Literal["error", "warning"]
RunState = Literal["pending", "running", "succeeded", "failed", "cancelled"]


class RunnerError(Exception):
    """Runner v2 生命周期方法的统一失败类型（prepare 装环境失败、launch 拿不到句柄等）。"""


@dataclass(frozen=True, slots=True)
class MaterialSpec:
    """物料契约的一项：后端要求实验工作区里必须/可选存在的文件。

    v1 把 requirements.txt/run.sh 硬编码在全局 validate_files 里；v2 里它们只是
    python-ml 后端的物料——openfoam 要 case 目录、ngspice 要 netlist，各自声明。
    """

    path: str  # 相对工作区路径（校验走 ssh_exec._validate_relpath 同款白名单）
    required: bool = True
    description: str = ""


@dataclass(frozen=True, slots=True)
class LicenseNeed:
    """License 席位需求（FlexLM 等建模为可调度资源：prepare 阶段排队获取而非报错）。"""

    feature: str  # License feature 名（如 "HFSS"）
    server: str = ""  # license server（host:port；空 = 后端默认）
    count: int = 1


@dataclass(frozen=True, slots=True)
class ResourceNeeds:
    """资源需求声明（None = 不声明该维度；调度/预检消费，R2 Resource 模型落地后生效）。"""

    cpu: int | None = None
    gpu: int | None = None
    mem_gb: float | None = None
    walltime_min: int | None = None


@dataclass(frozen=True, slots=True)
class RunnerManifest:
    """后端自述：注册表分派、物料校验、资源调度、prompt 组装都以它为准。"""

    backend: str  # 后端 id（注册表键，plan.backend 的合法取值）
    interaction: Interaction  # batch=一次跑完 / session=会话交互 / streaming=持续流式
    side_effects: SideEffects  # none/filesystem/network/physical（physical=动真设备）
    materials: tuple[MaterialSpec, ...] = ()  # 物料契约（validate 据此静态检查）
    io_schema: dict[str, Any] = field(default_factory=dict)  # 输入/输出形状的自述
    resources: ResourceNeeds = field(default_factory=ResourceNeeds)
    licenses: tuple[LicenseNeed, ...] = ()  # License 席位（python-ml 为空）
    credential_kinds: tuple[str, ...] = ("ssh",)  # 接受的凭据类型（R2 泛化后扩展）
    prompt_pack: str = ""  # plan/codegen/debug/reflect 提示词包引用（R3 消费）


@dataclass(frozen=True, slots=True)
class Issue:
    """validate 的产出：一条物料/计划问题（error 阻断，warning 提示）。"""

    code: str  # 机器可读（如 "materials.missing"）
    message: str
    path: str | None = None  # 关联的物料路径（有则填）
    severity: Severity = "error"


@dataclass(frozen=True, slots=True)
class RunStatus:
    """poll 的产出：运行状态快照。"""

    state: RunState
    exit_code: int | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ResultFile:
    """collect 结果文件通道的一项（v1 只有标量指标一条通道，这是 v2 的并行新增）。"""

    name: str  # 展示名（如 "primary_metric.png"）
    path: str  # 工作区内相对路径
    parser: str | None = None  # 用哪个解析器出摘要指标（如 "csv"；None=不解析）
    summary: dict[str, Any] | None = None  # 解析器产出的摘要（如 {"rows": 100}）


@dataclass(slots=True)
class ResultBundle:
    """collect 的产出：标量指标点 + 结果文件 + 备注。

    metrics 沿用现有标量时间序列点形状 [{name, step, value}]（DB/UI 承重面不动）；
    files 是新增的结果文件通道；notes 放人读的收尾说明（如日志尾部摘录）。
    """

    metrics: list[dict[str, Any]] = field(default_factory=list)
    files: list[ResultFile] = field(default_factory=list)
    notes: str = ""


@dataclass(slots=True)
class RunContext:
    """一次 run 的执行上下文：计划 + 物料内容 + 执行底座。

    ``substrate`` 是后端的执行底座——python-ml 传现有 Runner（SSH 主机/容器），
    将来 grpc 会话、本地进程各按后端约定传各自的底座；契约层不约束其类型，
    由具体 RunnerPlugin 自己收窄。``scratch`` 供插件在生命周期方法间传递私有状态
    （如 launch 记下启动命令供审计）。
    """

    plan: dict[str, Any] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)  # 物料：相对路径 → 文件内容
    substrate: Any = None
    scratch: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class RunnerPlugin(Protocol):
    """执行后端插件的生命周期契约（一个实例服务一次实验 run 的驱动）。

    约定：
    - ``cleanup`` 必然执行（成功/失败/取消都要走到——释放 License/设备归位靠它）；
    - 会话型后端另有 open_session/close_session，等第一个会话后端（pyansys/comsol）
      落地时再入契约；``dry_run`` 保留在契约中但暂缓启用（§13 拍板）。
    """

    manifest: RunnerManifest

    async def validate(self, plan: dict[str, Any]) -> list[Issue]:
        """物料契约静态校验：不碰远端，纯看 plan 与物料内容。空列表 = 可以往下走。"""
        ...

    async def prepare(self, ctx: RunContext) -> None:
        """环境/License/设备获取：建工作区、落物料、装依赖；失败抛 RunnerError。"""
        ...

    async def launch(self, ctx: RunContext) -> RunHandle:
        """启动运行，返回 str 句柄（后续 poll/cancel 凭它定位这次运行）。"""
        ...

    async def poll(self, handle: RunHandle) -> RunStatus:
        """查询运行状态（幂等、只读）。"""
        ...

    async def collect(self, ctx: RunContext) -> ResultBundle:
        """收集结果：标量指标 + 结果文件 + 备注（幂等，失败的 run 也要尽量收）。"""
        ...

    async def cancel(self, handle: RunHandle) -> None:
        """取消运行（尽力而为、幂等：已结束的 run 取消是 no-op）。"""
        ...

    async def cleanup(self, ctx: RunContext) -> None:
        """善后：断连接、释放 License/设备。必然执行，不得因 run 失败而跳过。"""
        ...

    async def dry_run(self, ctx: RunContext) -> RunStatus:
        """无副作用预演（§13 保留暂缓：契约占位，后端可 NotImplementedError）。"""
        ...
