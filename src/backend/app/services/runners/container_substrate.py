"""容器执行底座：本机 docker 上按**固定模板**拉起一次性批处理容器（#681，R4）。

ngspice/openfoam 等零 license 后端共用这一个底座。与 voyage/runner.py 的
ContainerRunner（SSH 主机 + docker exec 长驻容器）不同，这里是本机 docker 的
一次性批处理形态：`docker run -d --rm` 一个容器跑完即走，产物全部落在唯一
挂载的工作目录里。

安全铁律（v1/v2 一贯，任何调用方不得破坏——见 contract.py 模块 docstring）：
- **LLM 只产出文件内容**（netlist / OpenFOAM case …），经纯数据通道落盘；
- **进 docker CLI 的每个参数都来自 manifest/模板或白名单校验后的 plan 字段**
  （镜像名过 _IMAGE_RE、容器内命令是后端模块里的字符串常量模板、可变段
  只有白名单枚举如求解器名），LLM 自由文本**绝不**拼进命令行；
- host 侧不经过 shell：argv 列表直接 create_subprocess_exec，连引号注入的
  土壤都没有；容器内的 `sh -c` 只收固定模板串。

docker run 固定模板（side_effects=filesystem 的落实）：
- `--rm`：跑完即删，host 不留容器残骸（退出码经 run.exit 文件持久化，见下）；
- `--name <前缀>-<随机 hex>`：可定位、可取消、可按前缀清理；
- `--network none`（默认）：仿真不需要网络，直接断网——manifest 声明
  side_effects=filesystem 就真的只有文件系统一条副作用通道；
- 只挂工作目录（绝对路径 → /work，rw）：容器能读写的 host 文件面收敛到
  这一个目录，物料进、产物出都走它。

为什么退出码走 run.exit 文件而不是 docker inspect：`--rm` 让容器退出后自动
消失，inspect 会扑空。所以 launch 把后端模板包一层 `; echo $? > run.exit`
（照 python-ml 后端 run.exit 落盘的既有约定），poll 先问 inspect（容器还在 =
running / 刚退出），容器没了就读工作目录里的 run.exit 定终态。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path

from app.services.runners.contract import RunnerError, RunStatus
from app.services.ssh_exec import SSHPathViolationError, _validate_relpath

# 镜像引用白名单（与 voyage/runner.py 的 _IMAGE_RE 同款）：镜像名可来自 plan
# （LLM 产出），是唯一进 CLI 的 plan 字段，必须过正则才收。
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
# 容器名前缀：由后端模块写死（如 "polaris-ngspice"），仍校验兜一道
_NAME_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

_CONTAINER_WORKDIR = "/work"  # 工作目录在容器内的固定挂载点（与 ContainerRunner 一致）
RUN_EXIT_FILE = "run.exit"  # 退出码落盘文件（--rm 后 inspect 扑空时的终态依据）

_DOCKER_TIMEOUT = 60.0  # inspect/rm 等管理命令
_LAUNCH_TIMEOUT = 900.0  # docker run（镜像未预拉时含拉取；openfoam 镜像大，给足）


async def _run_docker(*argv: str, timeout: float = _DOCKER_TIMEOUT) -> tuple[int, str, str]:
    """执行一条 docker 命令（argv 直传，不过 host shell），返回 (exit, stdout, stderr)。"""
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        raise RunnerError(f"docker {argv[0]} 超时（>{timeout:.0f}s）") from None
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


class ContainerSubstrate:
    """一次 run 的本机容器底座：工作目录 + 拉起/查询/取消/读产物。

    实例绑定一个工作目录（绝对路径），不跨 run 复用。所有文件读写都限定在
    工作目录内（相对路径过 _validate_relpath 白名单，与 SSH 底座同款约束）。
    """

    def __init__(self, workdir: str | Path, *, name_prefix: str = "polaris-run") -> None:
        if not _NAME_PREFIX_RE.match(name_prefix):
            raise RunnerError(f"容器名前缀不合法：{name_prefix!r}")
        self._workdir = Path(workdir).resolve()  # docker -v 要绝对路径
        self._name_prefix = name_prefix

    @property
    def workdir(self) -> Path:
        return self._workdir

    # ---- 物料落盘（纯数据通道：LLM 产出的文件内容只走这里，不进命令行） ----

    def write_materials(self, files: dict[str, str]) -> None:
        """把物料（相对路径 → 文本内容）写进工作目录；路径过白名单防越界。"""
        self._workdir.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            try:
                rel = _validate_relpath(name)
            except SSHPathViolationError as e:
                raise RunnerError(str(e)) from e
            target = self._workdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    # ---- 生命周期 ----

    def _new_name(self) -> str:
        return f"{self._name_prefix}-{uuid.uuid4().hex[:12]}"

    async def launch(self, *, image: str, command: str) -> str:
        """按固定模板 docker run，返回容器名句柄。

        ``command`` 必须是后端模块里的字符串常量模板（可变段只有白名单枚举），
        由容器内 `sh -c` 执行；底座统一追加 `; echo $? > run.exit` 保证退出码
        在 --rm 之后仍可读。``image`` 过 _IMAGE_RE 白名单。
        """
        if not _IMAGE_RE.match(image or ""):
            raise RunnerError(f"容器镜像名不合白名单：{image!r}")
        if not self._workdir.is_dir():
            raise RunnerError(f"工作目录不存在（先 prepare）：{self._workdir}")
        name = self._new_name()
        # 固定模板：--rm 即跑即删 / 只挂工作目录 / 默认断网。参数没有一个来自自由文本。
        code, _out, err = await _run_docker(
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "-v",
            f"{self._workdir}:{_CONTAINER_WORKDIR}",
            "-w",
            _CONTAINER_WORKDIR,
            image,
            "sh",
            "-c",
            f"{command}; echo $? > {RUN_EXIT_FILE}",
            timeout=_LAUNCH_TIMEOUT,
        )
        if code != 0:
            raise RunnerError(f"docker run 启动失败：{err.strip()[:300]}")
        return name

    async def poll(self, handle: str) -> RunStatus:
        """inspect 在场容器；容器已被 --rm 收走则读工作目录 run.exit 定终态。"""
        code, out, _err = await _run_docker(
            "inspect", "-f", "{{.State.Running}} {{.State.ExitCode}}", handle
        )
        if code == 0:
            running, _, exit_str = out.strip().partition(" ")
            if running == "true":
                return RunStatus(state="running")
            # 已退出但尚未被 --rm 收走的小窗口：直接用 inspect 的退出码
            try:
                exit_code: int | None = int(exit_str)
            except ValueError:
                exit_code = None
            if exit_code == 0:
                return RunStatus(state="succeeded", exit_code=0)
            return RunStatus(state="failed", exit_code=exit_code)
        # 容器不在了（--rm 已收走，或被 cancel）：run.exit 是唯一可信终态
        exit_code = self.read_exit_code()
        if exit_code is None:
            return RunStatus(
                state="failed",
                detail="容器已消失且未落盘 run.exit（启动即崩、被强杀或已取消）",
            )
        if exit_code == 0:
            return RunStatus(state="succeeded", exit_code=0)
        return RunStatus(state="failed", exit_code=exit_code)

    async def cancel(self, handle: str) -> None:
        """docker rm -f：尽力而为、幂等（容器已结束/已删除都是 no-op）。"""
        await _run_docker("rm", "-f", handle)

    # ---- 产物读取（host 侧直读工作目录；不进容器） ----

    def read_exit_code(self) -> int | None:
        text = self.read_text(RUN_EXIT_FILE)
        if text is None:
            return None
        try:
            return int(text.strip())
        except ValueError:
            return None

    def read_text(self, relpath: str, *, max_bytes: int = 4 * 1024 * 1024) -> str | None:
        """读工作目录内文件（路径过白名单）；不存在返回 None，超长截断取前段。"""
        try:
            rel = _validate_relpath(relpath)
        except SSHPathViolationError as e:
            raise RunnerError(str(e)) from e
        target = self._workdir / rel
        if not target.is_file():
            return None
        return target.read_bytes()[:max_bytes].decode("utf-8", errors="replace")

    def tail(self, relpath: str, *, max_chars: int = 2000) -> str:
        """日志尾部（人读的收尾说明用）；文件不存在返回空串。"""
        text = self.read_text(relpath)
        return (text or "")[-max_chars:].strip()


def metric_point(name: str, step: int, value: float) -> dict[str, object]:
    """标量指标点，NaN 带 flag 记录而不丢弃。

    设计报告拍板（polaris-2.0 设计报告「实验执行后端」节 / p3 计划 R4）：
    仿真里 NaN 是**发散信号**，静默丢弃会把「解发散了」伪装成「没有数据」；
    记为 value=None + flag="nan"（NaN 进不了 JSON，用 None 占位、flag 定性）。
    """
    if value != value:  # NaN 唯一不等于自身
        return {"name": name, "step": step, "value": None, "flag": "nan"}
    return {"name": name, "step": step, "value": value}


def parse_float_table(text: str) -> tuple[list[str], list[list[float]]]:
    """解析空白分隔的数值表（wrdata 输出 / OpenFOAM postProcessing 的 dat/csv）。

    - ``#`` 开头的行是注释头；**最后一行**注释头当列名（OpenFOAM 惯例：
      ``# Time  p  U`` 紧贴数据）；无注释头则列名为空列表；
    - 数据行逐 token float()；含非标量 token 的行（如 OpenFOAM 向量列
      ``(0 0 0)``）整行跳过——本期只收标量列，向量场解析待后续后端需要再加；
    - ``nan``/``inf`` 是合法 float，**保留**进表（NaN 语义由 metric_point 定性）。
    """
    header: list[str] = []
    rows: list[list[float]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            fields = stripped.lstrip("#").replace(",", " ").split()
            if fields:
                header = fields
            continue
        tokens = stripped.replace(",", " ").split()
        try:
            rows.append([float(t) for t in tokens])
        except ValueError:
            continue  # 向量列/文字行：跳过（见 docstring）
    return header, rows


class ContainerBackendBase:
    """容器后端的公共生命周期（prepare/poll/cancel/cleanup/dry_run）。

    子类提供 manifest 并实现 validate/launch/collect；launch 必须把容器名
    句柄同时记入 ``ctx.scratch["container"]``（cleanup 兜底取消用）。
    """

    def __init__(self, substrate: ContainerSubstrate | None = None) -> None:
        self._substrate = substrate

    @classmethod
    def create(cls, *, substrate: ContainerSubstrate | None = None, **_: object):
        """注册表工厂入口（多余的底座关键字忽略：python-ml 等收的是 runner=）。"""
        return cls(substrate=substrate)

    def _require_substrate(self) -> ContainerSubstrate:
        if self._substrate is None:
            raise RunnerError("容器后端未绑定执行底座（resolve_backend 需传 substrate=…）")
        return self._substrate

    async def prepare(self, ctx) -> None:
        """备环境 = 建工作目录 + 物料落盘（纯数据通道）。

        资源租约（#680 → #716 已接线）：租约获取不在本底座内做，而在动作层
        experiment_setup 的备环境之前（resource_leases.wait_and_acquire，按
        checkpoint.params.resource_id 排队），释放挂在 voyage run 的终态写入点
        （release_for_run 兜底）——租约的主体是 run，底座不知道 run，放这里
        只会拿不到 run_id。manifest.resources 的 cpu/mem 需求供调度/预检消费。
        """
        substrate = self._require_substrate()
        if ctx.files:
            substrate.write_materials(ctx.files)
        else:
            substrate.workdir.mkdir(parents=True, exist_ok=True)

    async def poll(self, handle: str) -> RunStatus:
        return await self._require_substrate().poll(handle)

    async def cancel(self, handle: str) -> None:
        await self._require_substrate().cancel(handle)

    async def cleanup(self, ctx) -> None:
        """善后：兜底删掉可能还在跑的容器（幂等）。工作目录**有意**保留——
        与 python-ml 同理，用户复查产物、失败诊断都要它。"""
        handle = ctx.scratch.get("container")
        if handle and self._substrate is not None:
            await self._substrate.cancel(str(handle))

    async def dry_run(self, ctx) -> RunStatus:
        raise NotImplementedError("容器后端暂无 dry_run（契约占位，§13 暂缓启用）")
