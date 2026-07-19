"""Runner 抽象：实验的**可插拔执行后端**。

实验的 plan-execute-verify 循环只跟 `Runner` 打交道，不直接依赖 SSH——这样「在哪跑、怎么跑」
（裸机 venv / 容器 / 本地 / 纯 API）与「实验逻辑」解耦。一个 Runner 拥有实验的工作目录，并提供一组
**kind 无关**的原语：备环境、读写产物文件、跑实验入口（前台 + 后台带轮询）、流式日志。

今天唯一的实现是 `RemoteHostRunner`（在 SSH 主机上跑，即现有行为）；`ContainerRunner`（容器）、
`LocalRunner`（本地）、`ApiRunner`（纯 API 评测）以后按同一接口挂进来，
`open_runner` 按实验 kind 选。
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from app.models.ssh_credential import SSHCredential
from app.services.ssh_exec import (
    PLOT_TIMEOUT_SECONDS,
    SETUP_TIMEOUT_SECONDS,
    SMOKE_TIMEOUT_SECONDS,
    SSHExecutor,
    SSHResult,
    open_executor,
)

# 执行结果（沿用 SSH 层的结构；对上层是「exit_status/stdout/stderr」的通用运行结果）。
RunResult = SSHResult


@runtime_checkable
class Runner(Protocol):
    """实验执行后端的通用接口（kind 无关）。现有 SSHExecutor 结构上已满足它。

    入口约定与安全模型不变：LLM 只产出**文件内容**（run.sh / train.py / plot_figures.py 等），
    Runner 只跑**固定模板命令**（跑 run.sh、跑 plot_figures.py），可变的只有实验产物本身——
    因此「训练/评测/Agent」的差异体现在 LLM 写的文件里，而非让 LLM 拼 shell。
    """

    @property
    def workdir(self) -> str: ...

    # —— 工作区与产物 ——
    async def mkdir_workdir(self) -> RunResult: ...
    async def write_files(self, files: dict[str, str]) -> list[str]: ...
    async def read_file(self, relpath: str) -> bytes: ...
    async def list_dir(self, subdir: str) -> list[str]: ...
    async def read_metrics_json(self) -> str | None: ...

    # —— 备环境（裸机=venv+pip；容器实现里=拉镜像/准备镜像内环境）——
    async def setup_venv(self, timeout: float = SETUP_TIMEOUT_SECONDS) -> RunResult: ...

    # —— 跑实验入口（前台，冒烟/绘图用）——
    async def run_smoke(self, timeout: float = SMOKE_TIMEOUT_SECONDS) -> RunResult: ...
    async def run_plot(self, timeout: float = PLOT_TIMEOUT_SECONDS) -> RunResult: ...

    # —— 跑实验入口（后台脱离 + 轮询观测）——
    async def launch_run(self) -> tuple[int, str]: ...
    async def check_pid(self, pid: int) -> bool: ...
    async def read_exit_code(self) -> int | None: ...
    async def tail_log(self, offset: int = 0) -> tuple[str, int]: ...
    async def kill_pid(self, pid: int) -> RunResult: ...

    async def close(self) -> None: ...


# 现有实现：在 SSH 主机上跑裸机 venv 环境（即目前的全部行为）。
RemoteHostRunner = SSHExecutor


async def open_runner(
    *,
    credential: SSHCredential,
    exp_id: str | uuid.UUID,
    project_id: uuid.UUID,
    kind: str | None = None,  # noqa: ARG001 — 预留：按 kind 选 Runner（如 training→Container）
) -> Runner:
    """为一个实验挑选并打开 Runner。

    `kind` 是**预留的分派点**——以后训练类实验可返回 ContainerRunner、评测类返回 ApiRunner 等；
    目前所有 kind 都用 RemoteHostRunner，行为与之前完全一致。
    """
    return await open_executor(
        credential=credential, exp_id=str(exp_id), project_id=project_id
    )
