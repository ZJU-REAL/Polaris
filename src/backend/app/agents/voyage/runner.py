"""Runner 抽象：实验的**可插拔执行后端**。

实验的 plan-execute-verify 循环只跟 `Runner` 打交道，不直接依赖 SSH——这样「在哪跑、怎么跑」
（裸机 venv / 容器 / 本地 / 纯 API）与「实验逻辑」解耦。一个 Runner 拥有实验的工作目录，并提供一组
**kind 无关**的原语：备环境、读写产物文件、跑实验入口（前台 + 后台带轮询）、流式日志。

今天唯一的实现是 `RemoteHostRunner`（在 SSH 主机上跑，即现有行为）；`ContainerRunner`（容器）、
`LocalRunner`（本地）、`ApiRunner`（纯 API 评测）以后按同一接口挂进来，
`open_runner` 按实验 kind 选。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.models.ssh_credential import SSHCredential
from app.services.managed_commands import CommandSnapshot, OperationContext, RepairScope
from app.services.managed_ssh import (
    ManagedCommandHandle,
    ManagedGPUUsage,
    ManagedStopResult,
    OutputChunk,
)
from app.services.ssh_exec import (
    ENV_SOURCE_PREFIX,
    PLOT_TIMEOUT_SECONDS,
    SETUP_TIMEOUT_SECONDS,
    SMOKE_TIMEOUT_SECONDS,
    ManagedRuntimeUnavailableError,
    SSHExecError,
    SSHExecutor,
    SSHResult,
    SSHSession,
    open_executor,
    validate_managed_run_timeout,
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

    @property
    def experiment_workdir(self) -> str: ...

    @property
    def run_workspace_id(self) -> str | None: ...

    # —— 工作区与产物 ——
    def bind_run_workspace(self, run_id: str | None) -> None: ...
    async def prepare_run_workspace(self, run_id: str, files: dict[str, str]) -> str: ...
    async def mkdir_workdir(self) -> RunResult: ...
    async def write_files(self, files: dict[str, str]) -> list[str]: ...
    async def read_file(self, relpath: str) -> bytes: ...
    async def remove_candidate_files(self, paths: list[str]) -> None: ...
    async def list_dir(self, subdir: str) -> list[str]: ...
    async def read_metrics_json(self) -> str | None: ...

    # —— 备环境（裸机=venv+pip；容器实现里=拉镜像/准备镜像内环境）——
    async def setup_venv(self, timeout: float = SETUP_TIMEOUT_SECONDS) -> RunResult: ...

    # —— 备环境（后台脱离版：断连可重连接续跟踪同一安装进程）——
    async def launch_setup(self) -> tuple[int, str]: ...
    async def launch_managed_setup(
        self, *, attempt_id: str | None = None
    ) -> ManagedCommandHandle: ...
    async def prepare_managed(
        self, *, attempt_id: str | None = None
    ) -> ManagedCommandHandle | None: ...
    async def read_setup_exit(self) -> int | None: ...
    async def read_setup_log(self, tail_chars: int = 2000) -> str: ...

    # —— 跑实验入口（前台，冒烟/绘图用）——
    async def run_smoke(self, timeout: float = SMOKE_TIMEOUT_SECONDS) -> RunResult: ...
    async def launch_managed_smoke(
        self, *, attempt_id: str | None = None
    ) -> ManagedCommandHandle: ...
    async def run_plot(self, timeout: float = PLOT_TIMEOUT_SECONDS) -> RunResult: ...
    async def launch_managed_plot(
        self, *, attempt_id: str | None = None
    ) -> ManagedCommandHandle: ...
    async def launch_managed_plot_deps(
        self, *, attempt_id: str | None = None
    ) -> ManagedCommandHandle: ...
    async def ensure_plot_deps(self, timeout: float = SETUP_TIMEOUT_SECONDS) -> RunResult: ...

    # —— 资源预检（确定性探测；本机无 GPU/驱动 → 空列表）——
    async def probe_gpu(self) -> list[dict[str, int]]: ...
    async def host_path_exists(self, path: str) -> bool: ...
    async def read_host_file(self, path: str) -> str | None: ...

    # —— 跑实验入口（后台脱离 + 轮询观测）——
    async def launch_run(self) -> tuple[int, str]: ...
    async def launch_managed_run(
        self, timeout_seconds: float | None = None
    ) -> ManagedCommandHandle: ...
    async def recover_managed_command(
        self, context: OperationContext
    ) -> ManagedCommandHandle | None: ...
    async def inspect_managed_command(
        self,
        handle: ManagedCommandHandle,
        *,
        previous_token: str | None = None,
        diagnostic_evidence: dict[str, str] | None = None,
    ) -> CommandSnapshot: ...
    async def read_managed_output(
        self,
        handle: ManagedCommandHandle,
        *,
        stdout_offset: int = 0,
        stderr_offset: int = 0,
    ) -> tuple[list[OutputChunk], int, int]: ...
    async def diagnose_managed_command(self, handle: ManagedCommandHandle) -> dict[str, str]: ...
    async def managed_command_gpu_usage(self, handle: ManagedCommandHandle) -> ManagedGPUUsage: ...
    async def stop_managed_command(self, handle: ManagedCommandHandle) -> ManagedStopResult: ...
    async def check_pid(self, pid: int) -> bool: ...
    async def read_exit_code(self) -> int | None: ...
    async def tail_log(self, offset: int = 0) -> tuple[str, int]: ...
    async def kill_pid(self, pid: int) -> RunResult: ...

    async def close(self) -> None: ...


# 现有实现：在 SSH 主机上跑裸机 venv 环境（即目前的全部行为）。
# ⚠ non-ephemeral（#685）：裸机直跑会在主机留下 venv/产物/进程痕迹。BYO runner
# 语义下**推荐默认走容器化路径**（ContainerRunner，任务结束不残留）；裸机路径
# 保留给「用户明确接受残留」的场景（复查产物本就依赖 workdir 留存）。
RemoteHostRunner = SSHExecutor


# ---------------------------------------------------------------------------
# ContainerRunner：在 SSH 主机的 **docker 容器**里跑实验（训练类等「不重复造轮子、
# 直接用预置框架镜像」的场景）。同一 Runner 接口，只是把**执行命令**包一层 docker exec，
# 容器内挂载实验工作目录（host workdir ←bind→ 容器 /work）+ 模型/数据只读卷 + GPU 直通。
#
# 关键设计（为什么文件原语能原样复用 SSHExecutor）：
#   host 的 `~/polaris_runs/<exp>` 被 bind 挂进容器 /work；容器把 run.log/run.exit/metrics.json
#   写到 /work，在 host 侧即刻可见。所以 write_files / read_file / list_dir / tail_log /
#   read_exit_code / read_metrics_json / mkdir_workdir **全部继承**（走 host 侧，不进容器）；
#   只有真正「干活」的原语（setup/smoke/plot/launch/check_pid/kill_pid）改成在容器内执行。
# ---------------------------------------------------------------------------

# docker 相关字段的**严格白名单**（这些值来自 plan=LLM 产出，会拼进 docker 命令，必须防注入）。
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")  # docker 镜像引用合法字符
_GPUS_RE = re.compile(r"^(all|\d+|device=[\d,]+)$")  # all | 计数 | device=0,1
_SHM_RE = re.compile(r"^\d+[bkmgBKMG]?$")
_MOUNT_RE = re.compile(r"^[\w./~:-]+$")  # 卷路径：字母数字与 . / ~ : - _（禁空格/分号等）
_CONTAINER_WORKDIR = "/work"  # 实验工作目录在容器内的固定挂载点


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """一次实验的容器执行规格（由 plan.container 声明）。所有字段已过白名单校验。"""

    image: str
    gpus: str | None = None  # "all" | "device=0,1" | "2"(计数) | None(不透传 GPU)
    shm_size: str = "16g"
    # host→容器 的额外只读/读写卷（模型/数据集等，默认把 ~/hf 挂进去）。
    mounts: dict[str, str] = field(default_factory=lambda: {"~/hf": "/hf:ro"})
    workdir_mount: str = _CONTAINER_WORKDIR


def parse_container_spec(data: Any) -> ContainerSpec | None:
    """从 plan.container 解析并**严格校验**容器规格；无 image 或字段非法 → None（=退回裸机）。

    校验是安全边界：image/gpus/shm/mounts 会拼进 `docker run` 命令，任何不合白名单的值直接丢弃，
    绝不进 shell。返回 None 表示「这个实验不用容器」，上层用 RemoteHostRunner。
    """
    if not isinstance(data, dict):
        return None
    image = str(data.get("image") or "").strip()
    if not image or not _IMAGE_RE.match(image):
        return None
    gpus_raw = str(data.get("gpus") or "").strip()
    gpus = gpus_raw if gpus_raw and _GPUS_RE.match(gpus_raw) else None
    shm_raw = str(data.get("shm_size") or "").strip()
    shm = shm_raw if shm_raw and _SHM_RE.match(shm_raw) else "16g"
    mounts: dict[str, str] = {}
    raw_mounts = data.get("mounts")
    if isinstance(raw_mounts, dict):
        for host_path, ctr_path in raw_mounts.items():
            h, c = str(host_path).strip(), str(ctr_path).strip()
            if h and c and _MOUNT_RE.match(h) and _MOUNT_RE.match(c):
                mounts[h] = c
    if not mounts:
        mounts = {"~/hf": "/hf:ro"}
    return ContainerSpec(image=image, gpus=gpus, shm_size=shm, mounts=mounts)


class ContainerRunner(SSHExecutor):
    """在 SSH 主机的 docker 容器里跑实验。文件原语继承（host 侧），执行原语包一层 docker exec。

    BYO runner（#685）语境下这是**默认推荐**的执行形态（ephemeral 方向）：执行环境
    整体在容器里，主机侧只落 bind 挂载的 workdir。注册 runner 主机时
    config.ephemeral 默认 true 即指向此路径；分派开关本身仍是 plan.container
    （不改现有 run 行为，默认值只影响新注册的主机）。
    """

    CONTAINER_START_TIMEOUT = 600.0  # docker run（镜像已预拉时很快；给足冗余）

    def __init__(
        self,
        session: SSHSession,
        *,
        exp_id: str,
        host: str,
        project_id: uuid.UUID,
        spec: ContainerSpec,
        actor: str = "agent:experiment",
        proxy_url: str | None = None,
    ) -> None:
        super().__init__(
            session,
            exp_id=exp_id,
            host=host,
            project_id=project_id,
            actor=actor,
            proxy_url=proxy_url,
        )
        self._spec = spec

    @classmethod
    def from_executor(cls, base: SSHExecutor, *, spec: ContainerSpec) -> ContainerRunner:
        """复用一个已连接的 SSHExecutor（同一 SSH 会话）包成容器 Runner。"""
        wrapped = cls(
            base._session,
            exp_id=base.exp_id,
            host=base.host,
            project_id=base.project_id,
            spec=spec,
            actor=base.actor,
            proxy_url=base.proxy_url,
        )
        wrapped.bind_run_workspace(base.run_workspace_id)
        return wrapped

    # ---- docker 命令拼装（唯一进 shell 的容器命令来源；inner 为固定模板） ----

    @property
    def _container_name(self) -> str:
        return f"polaris_{self.exp_id}"  # exp_id 已过 validate_exp_id，docker name 安全

    def _dexec(self, inner: str) -> str:
        """把一段**固定模板** shell 命令包进 `docker exec ... bash -lc '...'`。"""
        if "'" in inner:  # 单引号会破坏包裹/有注入风险——模板里不该出现
            raise SSHExecError("容器命令模板不允许出现单引号")
        return f"docker exec {self._container_name} bash -lc '{inner}'"

    @property
    def _container_workdir(self) -> str:
        if self.run_workspace_id is not None:
            return f"{self._spec.workdir_mount}/.polaris/runs/{self.run_workspace_id}"
        return self._spec.workdir_mount

    def _dexec_workdir(self, inner: str) -> str:
        return self._dexec(f"cd {self._container_workdir} && {inner}")

    def _docker_run_cmd(self) -> str:
        spec = self._spec
        parts = ["docker run -d", f"--name {self._container_name}"]
        if spec.gpus == "all" or (spec.gpus and spec.gpus.isdigit()):
            parts.append(f"--gpus {spec.gpus}")
        elif spec.gpus:  # device=0,1 —— docker 需要 --gpus '"device=0,1"'
            parts.append(f"--gpus '\"{spec.gpus}\"'")
        parts.append(f"--shm-size {spec.shm_size}")
        for host_path, ctr_path in spec.mounts.items():
            parts.append(f"-v {host_path}:{ctr_path}")
        parts.append(f"-v {self.experiment_workdir}:{spec.workdir_mount}")  # host workdir ←→ /work
        parts.append(f"-w {spec.workdir_mount} {spec.image} tail -f /dev/null")
        return " ".join(parts)

    async def _ensure_container(self) -> None:
        """幂等：容器在跑就复用（断连重连友好）；否则清掉残留并重新 docker run。"""
        name = self._container_name
        probe = await self._run(f"docker inspect -f '{{{{.State.Running}}}}' {name} 2>/dev/null")
        if probe.stdout.strip() == "true":
            return
        await self._run(f"docker rm -f {name} >/dev/null 2>&1 || true")
        res = await self._run(self._docker_run_cmd(), timeout=self.CONTAINER_START_TIMEOUT)
        if res.exit_status != 0:
            detail = (res.stderr or res.stdout or "").strip()[:300]
            raise SSHExecError(f"docker run 启动容器失败：{detail}")

    async def prepare_managed(
        self, *, attempt_id: str | None = None
    ) -> ManagedCommandHandle | None:
        """Start container preparation through the generic command envelope."""
        name = self._container_name
        probe = await self._run(f"docker inspect -f '{{{{.State.Running}}}}' {name} 2>/dev/null")
        if probe.stdout.strip() == "true":
            return None
        await self._run(f"docker rm -f {name} >/dev/null 2>&1 || true")
        return await self.start_managed_command(
            OperationContext(
                phase="environment.prepare",
                operation="environment-prepare",
                display_command=f"start container from {self._spec.image}",
                target=self.host,
                soft_timeout_seconds=600,
                stall_timeout_seconds=900,
                repair_scope=RepairScope.INFRASTRUCTURE,
            ),
            self._docker_run_cmd(), attempt_id=attempt_id,
        )

    # ---- 执行原语（改成容器内执行；镜像自带框架，故不建 venv、用镜像 python） ----

    async def setup_venv(self, timeout: float = SETUP_TIMEOUT_SECONDS) -> SSHResult:
        """备环境：起容器 + 增量 pip 装 requirements.txt（镜像已含框架，无则跳过）。"""
        from app.core.config import get_settings

        await self._ensure_container()
        index = get_settings().pip_index_url
        index_arg = f" -i {index}" if index else ""
        inner = (
            f"{self._proxy_prefix()}"
            f"if [ -f requirements.txt ]; then pip install{index_arg} -r requirements.txt; "
            "else echo 'no requirements.txt: using image base'; fi"
        )
        return await self._run(self._dexec_workdir(inner), timeout=timeout)

    async def launch_setup(self) -> tuple[int, str]:
        """容器内后台装依赖（镜像已含框架，仅增量装 requirements）：setup.exit/setup.log 落 /work
        （=host workdir，read_setup_exit/read_setup_log 走 host 侧继承即可）。"""
        from app.core.config import get_settings

        await self._ensure_container()
        wd = self._container_workdir
        index = get_settings().pip_index_url
        index_arg = f" -i {index}" if index else ""
        # 启动脚本落 host workdir（=/work），内容无引号，避开 docker exec 引号嵌套。
        launcher = (
            f"cd {wd}\n"
            "rm -f setup.exit\n"
            f"{self._proxy_prefix()}\n"
            f"{{ if [ -f requirements.txt ]; then pip install{index_arg} -r requirements.txt; "
            "else echo 'no requirements.txt: using image base'; fi; } > setup.log 2>&1\n"
            "echo $? > setup.exit\n"
        )
        await self._session.write_file(f"{self._sftp_dir}/_setup_container.sh", launcher)
        command = self._dexec(
            f"cd {wd} && nohup bash _setup_container.sh >/dev/null 2>&1 & echo $!"
        )
        result = await self._run(command)
        try:
            pid = int(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as e:
            raise SSHExecError(f"launch_setup 未返回 PID：{result.stdout!r}") from e
        return pid, command

    async def launch_managed_setup(
        self, *, attempt_id: str | None = None
    ) -> ManagedCommandHandle:
        from app.core.config import get_settings

        await self._ensure_container()
        index = get_settings().pip_index_url
        index_arg = f" -i {index}" if index else ""
        inner = (
            f"{self._proxy_prefix()}"
            f"if [ -f requirements.txt ]; then pip install{index_arg} -r requirements.txt; "
            "else echo no requirements.txt: using image base; fi"
        )
        return await self._start_contained_command(
            OperationContext(
                phase="dependency.install",
                operation="dependency-install",
                display_command="install generated experiment dependencies",
                target=self.host,
                soft_timeout_seconds=600,
                stall_timeout_seconds=900,
                repair_scope=RepairScope.DEPENDENCY_FILES,
            ),
            inner, attempt_id=attempt_id,
        )

    async def run_smoke(self, timeout: float = SMOKE_TIMEOUT_SECONDS) -> SSHResult:
        await self._ensure_container()
        return await self._run(
            self._dexec_workdir(f"{{ {ENV_SOURCE_PREFIX} bash run.sh --smoke; }}"),
            timeout=timeout,
        )

    async def launch_managed_smoke(
        self, *, attempt_id: str | None = None
    ) -> ManagedCommandHandle:
        await self._ensure_container()
        return await self._start_contained_command(
            OperationContext(
                phase="application.smoke",
                operation="application-smoke",
                display_command="bash run.sh --smoke",
                target=self.host,
                soft_timeout_seconds=300,
                stall_timeout_seconds=600,
                repair_scope=RepairScope.APPLICATION_FILES,
            ),
            f"{{ {ENV_SOURCE_PREFIX} bash run.sh --smoke; }}", attempt_id=attempt_id,
        )

    async def run_plot(self, timeout: float = PLOT_TIMEOUT_SECONDS) -> SSHResult:
        await self._ensure_container()
        return await self._run(
            self._dexec_workdir(f"{{ {ENV_SOURCE_PREFIX} python plot_figures.py; }}"),
            timeout=timeout,
        )

    async def launch_managed_plot(
        self, *, attempt_id: str | None = None
    ) -> ManagedCommandHandle:
        await self._ensure_container()
        return await self._start_contained_command(
            OperationContext(
                phase="application.plot",
                operation="experiment-plot",
                display_command="python plot_figures.py",
                target=self.host,
                soft_timeout_seconds=PLOT_TIMEOUT_SECONDS,
                stall_timeout_seconds=600,
                hard_timeout_seconds=PLOT_TIMEOUT_SECONDS,
                repair_scope=RepairScope.APPLICATION_FILES,
            ),
            f"{{ {ENV_SOURCE_PREFIX} python plot_figures.py; }}", attempt_id=attempt_id,
        )

    async def ensure_plot_deps(self, timeout: float = SETUP_TIMEOUT_SECONDS) -> SSHResult:
        """容器版：镜像 python 缺 matplotlib 时增量装（幂等）。"""
        from app.core.config import get_settings

        await self._ensure_container()
        index = get_settings().pip_index_url
        index_arg = f" -i {index}" if index else ""
        inner = (
            f'{self._proxy_prefix()}python -c "import matplotlib" 2>/dev/null || '
            f"pip install{index_arg} matplotlib"
        )
        return await self._run(self._dexec_workdir(inner), timeout=timeout)

    async def launch_managed_plot_deps(
        self, *, attempt_id: str | None = None
    ) -> ManagedCommandHandle:
        from app.core.config import get_settings

        await self._ensure_container()
        index = get_settings().pip_index_url
        index_arg = f" -i {index}" if index else ""
        inner = (
            f'{self._proxy_prefix()}python -c "import matplotlib" 2>/dev/null || '
            f"pip install{index_arg} matplotlib"
        )
        return await self._start_contained_command(
            OperationContext(
                phase="dependency.plot",
                operation="plot-dependencies",
                display_command="ensure matplotlib is available",
                target=self.host,
                soft_timeout_seconds=600,
                stall_timeout_seconds=900,
                hard_timeout_seconds=SETUP_TIMEOUT_SECONDS,
                repair_scope=RepairScope.DEPENDENCY_FILES,
            ),
            inner, attempt_id=attempt_id,
        )

    async def launch_run(self) -> tuple[int, str]:
        """容器内后台启动正式运行。为避免 docker exec 的引号嵌套，把启动脚本落盘再 nohup 执行；
        返回容器命名空间内的 PID（check_pid/kill_pid 同样在容器内 kill，命名空间一致）。"""
        await self._ensure_container()
        wd = self._container_workdir
        # 启动脚本写到 host workdir（=容器 /work），内容无引号，避开 docker exec 的引号嵌套。
        launcher = (
            f"cd {wd}\n"
            "rm -f run.exit\n"
            "export PYTHONUNBUFFERED=1\n"
            f"{ENV_SOURCE_PREFIX}\n"
            "stdbuf -oL -eL bash run.sh > run.log 2>&1\n"
            "echo $? > run.exit\n"
        )
        await self._session.write_file(f"{self._sftp_dir}/_run_container.sh", launcher)
        # nohup 脱离 + 重定向到文件：docker exec 返回后被 reparent 到容器 init(tail -f)，继续跑。
        command = self._dexec(f"cd {wd} && nohup bash _run_container.sh >/dev/null 2>&1 & echo $!")
        result = await self._run(command)
        try:
            pid = int(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as e:
            raise SSHExecError(f"launch_run 未返回 PID：{result.stdout!r}") from e
        return pid, command

    async def launch_managed_run(
        self, timeout_seconds: float | None = None
    ) -> ManagedCommandHandle:
        seconds = validate_managed_run_timeout(timeout_seconds)
        await self._ensure_container()
        deadline = ""
        if seconds is not None:
            available = await self._run(self._dexec("command -v timeout >/dev/null 2>&1"))
            if available.exit_status != 0:
                raise ManagedRuntimeUnavailableError("timeout", target=self.host, contained=True)
            deadline = f"timeout --signal=TERM --kill-after=5s {seconds:.6f}s "
        inner = (f"export PYTHONUNBUFFERED=1; {ENV_SOURCE_PREFIX} "
                 f"{deadline}stdbuf -oL -eL bash run.sh")
        return await self._start_contained_command(
            OperationContext(
                phase="application.run",
                operation="experiment-run",
                display_command="bash run.sh",
                target=self.host,
                soft_timeout_seconds=600,
                stall_timeout_seconds=900,
                hard_timeout_seconds=seconds,
                repair_scope=RepairScope.APPLICATION_FILES,
            ),
            inner,
        )

    async def stop_managed_command(self, handle: ManagedCommandHandle) -> ManagedStopResult:
        """Host docker-exec termination cannot prove the contained job stopped."""
        host_stop = await super().stop_managed_command(handle)
        if not host_stop:
            return host_stop
        name = self._container_name
        await self._run(f"docker stop -t 5 {name} >/dev/null 2>&1", timeout=60)
        probe = await self._run(f"docker inspect -f '{{{{.State.Running}}}}' {name} 2>/dev/null")
        if probe.exit_status == 0 and probe.stdout.strip() == "false":
            return ManagedStopResult(status="container_stopped", confirmed=True)
        if probe.exit_status != 0:
            present = await self._run(
                f"docker container ls -a --filter name=^{name}$ --format '{{{{.ID}}}}' 2>/dev/null"
            )
            if present.exit_status == 0 and not present.stdout.strip():
                return ManagedStopResult(status="container_absent", confirmed=True)
        return ManagedStopResult(status="container_stop_unconfirmed", confirmed=False)

    @staticmethod
    def _contained_prefix(operation_id: str, attempt_id: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", operation_id):
            raise SSHExecError("invalid contained operation identity")
        attempt = str(uuid.UUID(attempt_id))
        return f".polaris/container/{operation_id}/{attempt}"

    async def _start_contained_command(
        self, context: OperationContext, inner: str, *, attempt_id: str | None = None
    ) -> ManagedCommandHandle:
        """Persist the contained process group alongside the durable host attempt."""
        available = await self._run(self._dexec("command -v setsid >/dev/null 2>&1"))
        if available.exit_status != 0:
            raise ManagedRuntimeUnavailableError("setsid", target=self.host, contained=True)
        attempt_id = str(uuid.UUID(attempt_id)) if attempt_id else str(uuid.uuid4())
        prefix = self._contained_prefix(context.operation, attempt_id)
        created = await self._run(f"mkdir -p {self.workdir}/.polaris/container/{context.operation}")
        if created.exit_status != 0:
            raise SSHExecError("contained command identity directory could not be created")
        launcher = (
            "#!/usr/bin/env bash\nset +e\n"
            f"prefix={prefix}\n"
            "echo $$ > ${prefix}.pid\n"
            "ps -o pgid= -p $$ | tr -d ' ' > ${prefix}.pgid\n"
            "{\n"
            f"{inner}\n"
            "}\n"
            "status=$?\nprintf '%s\\n' \"$status\" > ${prefix}.exit\nexit $status\n"
        )
        await self._session.write_file(f"{self._sftp_dir}/{prefix}.sh", launcher)
        return await self.start_managed_command(
            context, self._dexec_workdir(f"setsid bash {prefix}.sh"), attempt_id=attempt_id,
        )

    async def _contained_process_state(self, handle: ManagedCommandHandle) -> str:
        probe = await self._run(
            f"docker inspect -f '{{{{.State.Running}}}}' {self._container_name} 2>/dev/null"
        )
        if probe.exit_status == 0 and probe.stdout.strip() == "false":
            return "exited"
        if probe.exit_status != 0 or probe.stdout.strip() != "true":
            return "unknown"
        prefix = self._contained_prefix(handle.operation_id, handle.attempt_id)
        recorded = await self._run(
            self._dexec(f"cat {self._container_workdir}/{prefix}.pgid 2>/dev/null")
        )
        try:
            pgid = int(recorded.stdout.strip())
        except (TypeError, ValueError):
            return "unknown"
        if recorded.exit_status != 0 or pgid <= 0:
            return "unknown"
        members = await self._run(self._dexec("ps -eo pgid=,stat= 2>/dev/null"))
        if members.exit_status != 0:
            return "unknown"
        for line in members.stdout.splitlines():
            fields = line.split()
            try:
                if int(fields[0]) == pgid and not fields[1].startswith("Z"):
                    return "running"
            except (ValueError, IndexError):
                continue
        return "exited"

    async def inspect_managed_command(
        self,
        handle: ManagedCommandHandle,
        *,
        previous_token: str | None = None,
        diagnostic_evidence: dict[str, str] | None = None,
    ) -> CommandSnapshot:
        snapshot = await super().inspect_managed_command(
            handle, previous_token=previous_token, diagnostic_evidence=diagnostic_evidence
        )
        if (
            handle.operation_id in {"experiment-run", "application-smoke", "dependency-install"}
            and (snapshot.exit_status is not None or not snapshot.process_alive)
        ):
            contained = await self._contained_process_state(handle)
            if contained != "exited":
                snapshot.process_alive = True
                snapshot.process_state = f"contained_process_{contained}"
        return snapshot

    async def check_pid(self, pid: int) -> bool:
        result = await self._run(
            self._dexec(f"kill -0 {int(pid)} 2>/dev/null && echo alive || echo dead")
        )
        return "alive" in result.stdout

    async def kill_pid(self, pid: int) -> SSHResult:
        return await self._run(self._dexec(f"kill {int(pid)} 2>/dev/null || true"))


async def open_runner(
    *,
    credential: SSHCredential,
    exp_id: str | uuid.UUID,
    project_id: uuid.UUID,
    kind: str | None = None,  # noqa: ARG001 — kind 是提示；具体分派看 plan 是否声明 container
    container: Any = None,
) -> Runner:
    """为一个实验挑选并打开 Runner。

    分派规则（声明式，kind 无关）：plan 若声明了合法的 `container`（含 image）→ ContainerRunner
    （在容器里跑，训练类/需框架的实验用）；否则 → RemoteHostRunner（裸机 venv，行为与之前一致）。
    `kind` 只是提示（PLAN 用它决定要不要声明 container），真正的开关是 container 规格本身。
    """
    executor = await open_executor(credential=credential, exp_id=str(exp_id), project_id=project_id)
    spec = parse_container_spec(container)
    if spec is not None:
        return ContainerRunner.from_executor(executor, spec=spec)
    return executor
