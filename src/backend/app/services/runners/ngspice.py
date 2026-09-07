"""ngspice 后端：零 license 电路仿真容器（#681，R4；p3 计划的第一档验证后端）。

为什么选它打头阵：ngspice 开源零 license、镜像体积小（CI 拉得动）、批处理
模式一条命令跑完——正好用最小代价验证 v2 的物料契约与结果文件通道。

物料契约：一个 SPICE netlist ``circuit.cir``（LLM 产出**文件内容**，经纯数据
通道落盘）。执行：固定模板 ``ngspice -b circuit.cir -o run.log``（-b 批处理，
-o 全部输出进 run.log），模板是本模块字符串常量，plan 只可能改**镜像名**
（过白名单正则）——LLM 自由文本永不进 CLI（铁律见 container_substrate.py）。

结果双通道：
- run.log 里的 ``name = value`` 行（.meas 测量结果 / .op 工作点）→ 标量指标；
- netlist 里 ``wrdata <file> <vec…>`` 声明的数据文件 → 指标序列 + 结果文件。
NaN 带 flag 记录不丢弃（发散信号，见 metric_point 的 docstring）。
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
    RunnerManifest,
)

NGSPICE_BACKEND = "ngspice"

NETLIST = "circuit.cir"
RUN_LOG = "run.log"

# 默认镜像：平台自维护的公共 ngspice 镜像（docker hub 上没有官方/权威镜像，
# 不能默认信任个人镜像；构建配方就是 alpine + apk add ngspice，见 #681）。
# plan.image 可覆盖（过 _IMAGE_RE 白名单），CI/本地冒烟用本地构建的 tag。
DEFAULT_IMAGE = "polaris/ngspice:latest"

# 固定模板命令（唯一进容器 sh -c 的内容；无任何 plan/LLM 插值）
LAUNCH_TEMPLATE = f"ngspice -b {NETLIST} -o {RUN_LOG}"

# run.log 里的标量结果行：.meas 打印 "vmax = 4.99e+00 at= …"、.op 打印
# "v(out) = 2.5e+00"。名字收字母开头的标识符（可含括号/点），值交给 float()
# 判定（nan/inf 也是合法 float——发散要记下来，不是丢掉）。
_SCALAR_LINE_RE = re.compile(r"^\s*([A-Za-z_][\w().\[\]#]*)\s*=\s*([-+\w.]+)")
# netlist 里的 wrdata 声明：wrdata <文件名> <向量…>（.control 块内，大小写不敏感）
_WRDATA_RE = re.compile(r"^\s*wrdata\s+(\S+)\s+(.+?)\s*$", re.IGNORECASE | re.MULTILINE)

# 单个序列的指标点上限：长瞬态几十万行全进 DB 没有意义，等步长抽稀到上限内
# （首尾保留由抽稀步长自然覆盖不了尾点的场景可接受——曲线形态是目的）。
_MAX_SERIES_POINTS = 2000

NGSPICE_MANIFEST = RunnerManifest(
    backend=NGSPICE_BACKEND,
    interaction="batch",
    side_effects="filesystem",  # 容器 --network none + 只挂工作目录，声明与落实一致
    materials=(
        MaterialSpec(
            NETLIST,
            required=True,
            description="SPICE netlist（首行必须是标题行；.control 块内可用 wrdata 导出序列）",
        ),
    ),
    io_schema={
        "inputs": {
            NETLIST: "LLM 产出的电路 netlist",
            "plan.image": f"可选容器镜像（白名单校验；默认 {DEFAULT_IMAGE}）",
        },
        "outputs": {
            RUN_LOG: "ngspice 批处理全部输出（.meas/.op 标量在此解析）",
            "wrdata 文件": "netlist 里 wrdata 声明的数据表 → 指标序列 + 结果文件",
        },
    },
    # 单进程 CPU 仿真的保守声明；#680 租约接线见 ContainerBackendBase.prepare 的 TODO
    resources=ResourceNeeds(cpu=1, mem_gb=2.0, walltime_min=60),
    licenses=(),  # 零 license 正是选它的原因
    credential_kinds=(),  # 本机 docker，无需连接凭据
    prompt_pack=NGSPICE_BACKEND,
)


class NgspiceRunner(ContainerBackendBase):
    """ngspice 批处理容器后端（生命周期公共部分见 ContainerBackendBase）。"""

    manifest = NGSPICE_MANIFEST

    async def validate(self, plan: dict[str, Any]) -> list[Issue]:
        """netlist 静态检查：存在、非空、首行是标题行（ngspice 无条件把首行当
        标题——首行若是 ``.`` 指令会被整行吞掉，仿真悄悄错，必须挡在这里）。"""
        files = plan.get("files") if isinstance(plan, dict) else None
        if not isinstance(files, dict) or NETLIST not in files:
            return [
                Issue(
                    code="materials.missing",
                    message=f"缺少必需物料 {NETLIST}（SPICE netlist）",
                    path=NETLIST,
                )
            ]
        netlist = files[NETLIST]
        lines = [ln for ln in str(netlist).splitlines() if ln.strip()]
        if not lines:
            return [
                Issue(
                    code="materials.empty",
                    message=f"{NETLIST} 为空（netlist 至少要有标题行与 .end）",
                    path=NETLIST,
                )
            ]
        issues: list[Issue] = []
        first = lines[0].strip()
        if first.startswith("."):
            issues.append(
                Issue(
                    code="materials.invalid",
                    message=(
                        f"{NETLIST} 首行是 {first.split()[0]} 指令——ngspice 会把首行"
                        "当标题吞掉，该指令不会生效；请在首行加一行标题"
                    ),
                    path=NETLIST,
                )
            )
        if not any(ln.strip().lower().startswith(".end") for ln in lines):
            issues.append(
                Issue(
                    code="materials.suspicious",
                    message=f"{NETLIST} 未见 .end——ngspice 可能报 netlist 不完整",
                    path=NETLIST,
                    severity="warning",
                )
            )
        return issues

    async def launch(self, ctx: RunContext) -> RunHandle:
        """固定模板拉起批处理容器；镜像名是 plan 唯一可变项（底座白名单校验）。"""
        substrate = self._require_substrate()
        image = str(ctx.plan.get("image") or "").strip() or DEFAULT_IMAGE
        handle = await substrate.launch(image=image, command=LAUNCH_TEMPLATE)
        ctx.scratch["container"] = handle  # cleanup 兜底取消用
        ctx.scratch["launch_command"] = LAUNCH_TEMPLATE  # 审计留档（照 python-ml 惯例）
        return handle

    async def collect(self, ctx: RunContext) -> ResultBundle:
        """收结果：run.log 标量行 + wrdata 序列 + 结果文件登记（失败的 run 也尽量收）。"""
        substrate = self._require_substrate()
        metrics: list[dict[str, Any]] = []
        files: list[ResultFile] = []

        log_text = substrate.read_text(RUN_LOG)
        if log_text is not None:
            files.append(ResultFile(name=RUN_LOG, path=RUN_LOG))
            # 标量结果（.meas/.op）没有时间轴，step 恒 0（一次 run 一个值）
            for line in log_text.splitlines():
                m = _SCALAR_LINE_RE.match(line)
                if not m:
                    continue
                try:
                    value = float(m.group(2))
                except ValueError:
                    continue  # "= 号后不是数"的普通文案行（如 ngspice 的提示语）
                metrics.append(metric_point(m.group(1), 0, value))

        # wrdata 声明来自 netlist 物料本身（确定性解析，不是 LLM 再猜一遍）
        netlist = ctx.files.get(NETLIST) or substrate.read_text(NETLIST) or ""
        for m in _WRDATA_RE.finditer(netlist):
            filename, names = m.group(1), m.group(2).split()
            table_text = substrate.read_text(filename)
            if table_text is None:
                continue  # 仿真中途失败可能没写出来；标量通道照收
            _, rows = parse_float_table(table_text)
            metrics.extend(self._series_points(names, rows))
            files.append(
                ResultFile(
                    name=filename,
                    path=filename,
                    parser="table",
                    summary={"rows": len(rows), "columns": len(rows[0]) if rows else 0},
                )
            )
        return ResultBundle(metrics=metrics, files=files, notes=substrate.tail(RUN_LOG))

    @staticmethod
    def _series_points(names: list[str], rows: list[list[float]]) -> list[dict[str, Any]]:
        """wrdata 表 → 指标序列。列布局按 ngspice 约定判定：

        - 默认每个向量带自己的横轴：2N 列 = (scale, value) 对，取奇数列；
        - ``set wr_singlescale`` 时 N+1 列 = 共享横轴 + N 个值列；
        - 其余布局（手工文件等）退化为「首列横轴、其余按序配名」。
        step 用行号（横轴数值是物理时间，指标点契约的 step 是 int 序号）。
        """
        if not rows or not names:
            return []
        ncols = len(rows[0])
        if ncols == 2 * len(names):
            cols = [2 * i + 1 for i in range(len(names))]
        elif ncols == len(names) + 1:
            cols = list(range(1, ncols))
        else:
            cols = list(range(1, min(ncols, len(names) + 1)))
        stride = max(1, -(-len(rows) // _MAX_SERIES_POINTS))  # ceil，抽稀到上限内
        points: list[dict[str, Any]] = []
        for step, row in enumerate(rows):
            if step % stride:
                continue
            for name, col in zip(names, cols, strict=False):
                if col < len(row):
                    points.append(metric_point(name, step, row[col]))
        return points


def create_substrate(workdir: str, **kwargs: Any) -> ContainerSubstrate:
    """便捷工厂：给动作层/冒烟测试建一个带 ngspice 前缀的底座。"""
    return ContainerSubstrate(workdir, name_prefix=f"polaris-{NGSPICE_BACKEND}", **kwargs)
