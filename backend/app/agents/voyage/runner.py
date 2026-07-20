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
from app.services.ssh_exec import (
    ENV_SOURCE_PREFIX,
    PLOT_TIMEOUT_SECONDS,
    SETUP_TIMEOUT_SECONDS,
    SMOKE_TIMEOUT_SECONDS,
    SSHExecError,
    SSHExecutor,
    SSHResult,
    SSHSession,
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

    # —— 资源预检（确定性探测；本机无 GPU/驱动 → 空列表）——
    async def probe_gpu(self) -> list[dict[str, int]]: ...

    # —— 跑实验入口（后台脱离 + 轮询观测）——
    async def launch_run(self) -> tuple[int, str]: ...
    async def check_pid(self, pid: int) -> bool: ...
    async def read_exit_code(self) -> int | None: ...
    async def tail_log(self, offset: int = 0) -> tuple[str, int]: ...
    async def kill_pid(self, pid: int) -> RunResult: ...

    async def close(self) -> None: ...


# 现有实现：在 SSH 主机上跑裸机 venv 环境（即目前的全部行为）。
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
    """在 SSH 主机的 docker 容器里跑实验。文件原语继承（host 侧），执行原语包一层 docker exec。"""

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
        return cls(
            base._session,
            exp_id=base.exp_id,
            host=base.host,
            project_id=base.project_id,
            spec=spec,
            actor=base.actor,
            proxy_url=base.proxy_url,
        )

    # ---- docker 命令拼装（唯一进 shell 的容器命令来源；inner 为固定模板） ----

    @property
    def _container_name(self) -> str:
        return f"polaris_{self.exp_id}"  # exp_id 已过 validate_exp_id，docker name 安全

    def _dexec(self, inner: str) -> str:
        """把一段**固定模板** shell 命令包进 `docker exec ... bash -lc '...'`。"""
        if "'" in inner:  # 单引号会破坏包裹/有注入风险——模板里不该出现
            raise SSHExecError("容器命令模板不允许出现单引号")
        return f"docker exec {self._container_name} bash -lc '{inner}'"

    def _dexec_workdir(self, inner: str) -> str:
        return self._dexec(f"cd {self._spec.workdir_mount} && {inner}")

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
        parts.append(f"-v {self.workdir}:{spec.workdir_mount}")  # host workdir ←→ /work
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

    async def run_smoke(self, timeout: float = SMOKE_TIMEOUT_SECONDS) -> SSHResult:
        await self._ensure_container()
        return await self._run(
            self._dexec_workdir(f"{{ {ENV_SOURCE_PREFIX} bash run.sh --smoke; }}"),
            timeout=timeout,
        )

    async def run_plot(self, timeout: float = PLOT_TIMEOUT_SECONDS) -> SSHResult:
        await self._ensure_container()
        return await self._run(
            self._dexec_workdir(f"{{ {ENV_SOURCE_PREFIX} python plot_figures.py; }}"),
            timeout=timeout,
        )

    async def launch_run(self) -> tuple[int, str]:
        """容器内后台启动正式运行。为避免 docker exec 的引号嵌套，把启动脚本落盘再 nohup 执行；
        返回容器命名空间内的 PID（check_pid/kill_pid 同样在容器内 kill，命名空间一致）。"""
        await self._ensure_container()
        wd = self._spec.workdir_mount
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
