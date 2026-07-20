"""SSH 执行层（M4 关键安全组件，docs/api-m4.md §3 安全约束）。

设计要点：
- 连接层抽象为可注入接口（``SSHConnector``/``SSHSession``），测试注入内存 fake，
  生产用 ``AsyncsshConnector``（known_hosts=None，但把服务器 host key 指纹记入日志审计）；
- **命令白名单模板**：LLM 只产出文件内容，永远不拼 shell。所有远程命令由本模块的
  固定 f-string 模板生成，可变参数只有 exp_id（强校验 UUID）、pid/offset（强转 int）、
  文件相对路径（``_validate_relpath`` 拒绝绝对路径 / ``..`` / ``~``）；
- 工作目录限定 ``~/polaris_runs/<exp_id>``，文件一律经 SFTP 写入该目录之下；
- 每条远程命令写审计：Activity(kind="ssh.exec", payload={host, command}) + 专用 logger。
"""

import logging
import posixpath
import re
import uuid
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Protocol

from app.core.db import get_sessionmaker
from app.core.security import decrypt_secret
from app.models.activity import Activity
from app.models.ssh_credential import SSHCredential

logger = logging.getLogger("polaris.ssh")

WORKDIR_ROOT_SHELL = "~/polaris_runs"  # shell 命令用（~ 由远端展开）
WORKDIR_ROOT_SFTP = "polaris_runs"  # SFTP 相对 home 目录
SMOKE_TIMEOUT_SECONDS = 600.0  # 冒烟测试 10 分钟上限
SETUP_TIMEOUT_SECONDS = 1800.0  # venv + pip install 30 分钟上限
PLOT_TIMEOUT_SECONDS = 300.0  # plot_figures.py 5 分钟上限（docs/api-m5-a.md §1）
DEFAULT_CMD_TIMEOUT_SECONDS = 60.0

# 白名单模板内的固定前缀（无任何可变参数，无注入面）：workdir 下若有平台生成的
# env.sh（POLARIS_WORKDIR / 可选 HF_ENDPOINT 等）则先 source，再执行实验命令。
ENV_SOURCE_PREFIX = "[ -f env.sh ] && . ./env.sh;"

_PROXY_URL_RE = re.compile(r"^https?://[A-Za-z0-9.\-]+(:\d+)?$")


def validate_proxy_url(url: str | None) -> str | None:
    """代理 URL 严格校验（该值会拼进远端 shell 的 export，格式外一律拒绝）。"""
    if not url:
        return None
    url = url.strip()
    if not _PROXY_URL_RE.match(url):
        raise SSHExecError(f"代理地址格式非法：{url!r}")
    return url


class SSHExecError(Exception):
    """SSH 执行层错误基类。"""


class SSHPathViolationError(SSHExecError):
    """路径越界：目标不在 ~/polaris_runs/<exp_id> 之下。"""


def is_connection_error(exc: BaseException) -> bool:
    """判定异常是否为「SSH 连接/通道断开」这类可重连的瞬时故障。

    长实验轮询期间底层连接可能被服务器 idle 断开或网络抖动切断（asyncssh 抛
    ChannelOpenError/ConnectionLost 等）。这类故障是瞬时的、可重连的——远端运行状态
    （run.exit/run.log/pid）都持久化在服务器上，重连后可继续跟踪，不该让整个实验失败。
    与之相对，命令执行本身的错误（SSHExecError/路径越界）不属于此类。"""
    if isinstance(exc, SSHExecError):
        return False
    if isinstance(exc, (ConnectionError, EOFError, TimeoutError, OSError)):
        return True
    try:
        import asyncssh

        if isinstance(exc, asyncssh.Error):
            return True
    except Exception:  # noqa: BLE001 — asyncssh 不可用时退回文本判定
        pass
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        kw in text
        for kw in ("connection closed", "connection lost", "channel", "broken pipe", "disconnect")
    )


@dataclass(slots=True)
class SSHResult:
    exit_status: int
    stdout: str
    stderr: str


class SSHSession(Protocol):
    """单个 SSH 连接（测试用内存 fake 实现同一协议）。"""

    async def run(self, command: str, timeout: float | None = None) -> SSHResult: ...

    async def write_file(self, path: str, content: str) -> None: ...

    async def read_file(self, path: str) -> bytes: ...

    async def close(self) -> None: ...


class SSHConnector(Protocol):
    async def connect(
        self,
        *,
        host: str,
        port: int,
        username: str,
        private_key: str,
        passphrase: str | None = None,
    ) -> SSHSession: ...


# ---- asyncssh 真实实现（离线测试不触达） ----


class AsyncsshSession:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def run(self, command: str, timeout: float | None = None) -> SSHResult:
        result = await self._conn.run(command, check=False, timeout=timeout)
        return SSHResult(
            exit_status=int(result.exit_status or 0),
            stdout=str(result.stdout or ""),
            stderr=str(result.stderr or ""),
        )

    async def write_file(self, path: str, content: str) -> None:
        async with self._conn.start_sftp_client() as sftp, sftp.open(path, "w") as f:
            await f.write(content)

    async def read_file(self, path: str) -> bytes:
        async with self._conn.start_sftp_client() as sftp, sftp.open(path, "rb") as f:
            return await f.read()

    async def close(self) -> None:
        self._conn.close()
        await self._conn.wait_closed()


class AsyncsshConnector:
    """known_hosts=None（实验室内网机器无集中 known_hosts），host key 指纹记日志审计。"""

    async def connect(
        self,
        *,
        host: str,
        port: int,
        username: str,
        private_key: str,
        passphrase: str | None = None,
    ) -> SSHSession:
        import asyncssh

        key = asyncssh.import_private_key(private_key, passphrase=passphrase)
        conn = await asyncssh.connect(
            host, port=port, username=username, client_keys=[key], known_hosts=None
        )
        host_key = conn.get_server_host_key()
        fingerprint = host_key.get_fingerprint() if host_key is not None else "unknown"
        logger.info(
            "ssh.connect host=%s port=%s user=%s hostkey=%s", host, port, username, fingerprint
        )
        return AsyncsshSession(conn)


# ---- 连接器注入（测试替换为内存 fake） ----

_connector_factory: Any = None


def get_connector() -> SSHConnector:
    if _connector_factory is not None:
        return _connector_factory()
    return AsyncsshConnector()


def set_connector_factory(factory: Any) -> None:
    """测试用：注入 fake 连接器工厂；传 None 恢复 asyncssh 实现。"""
    global _connector_factory
    _connector_factory = factory


# ---- 路径与参数强校验 ----


def validate_exp_id(exp_id: str) -> str:
    """exp_id 必须是合法 UUID（防止拼进命令的路径段被注入）。"""
    return str(uuid.UUID(str(exp_id)))


# 主机绝对/家目录路径白名单（资源预检 cat/test 模型或数据集路径，值来自 plan=LLM，须防注入）。
_HOST_PATH_RE = re.compile(r"^[A-Za-z0-9~/][A-Za-z0-9._/~@-]*$")


def validate_host_path(path: str) -> str:
    """校验主机路径（禁 shell 元字符与 ..，允许前导 ~ // 与 HF id 风格的 /）。"""
    p = str(path).strip()
    if not p or ".." in p or not _HOST_PATH_RE.match(p):
        raise SSHExecError(f"非法主机路径：{path!r}")
    return p


def parse_gpu_csv(stdout: str) -> list[dict[str, int]]:
    """解析 nvidia-smi CSV（index,memory.total,memory.free；noheader,nounits）为每卡 dict。

    非法/不完整行跳过（容错解析），空输入 → 空列表。
    """
    gpus: list[dict[str, int]] = []
    for line in stdout.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            idx, total, free = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        gpus.append({"index": idx, "mem_total_mib": total, "mem_free_mib": free})
    return gpus


def workdir_for(exp_id: str) -> str:
    return f"{WORKDIR_ROOT_SHELL}/{validate_exp_id(exp_id)}"


def _validate_relpath(name: str) -> str:
    """文件相对路径校验：拒绝绝对路径 / ~ / .. / 空路径，返回规范化相对路径。"""
    raw = str(name).strip()
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or raw.startswith("~")
        or any(part in ("..", "", "~") for part in path.parts)
    ):
        raise SSHPathViolationError(f"文件路径越界（须为 workdir 内相对路径）：{name!r}")
    normalized = posixpath.normpath(str(path))
    if normalized.startswith("..") or normalized.startswith("/"):
        raise SSHPathViolationError(f"文件路径越界（须为 workdir 内相对路径）：{name!r}")
    return normalized


async def open_executor(
    *,
    credential: SSHCredential,
    exp_id: str,
    project_id: uuid.UUID,
) -> "SSHExecutor":
    """解密凭据 → 建连 → 返回绑定实验工作目录的执行器。"""
    private_key = decrypt_secret(credential.private_key_encrypted)
    passphrase = (
        decrypt_secret(credential.passphrase_encrypted) if credential.passphrase_encrypted else None
    )
    session = await get_connector().connect(
        host=credential.host,
        port=credential.port,
        username=credential.username,
        private_key=private_key,
        passphrase=passphrase,
    )
    return SSHExecutor(
        session,
        exp_id=exp_id,
        host=credential.host,
        project_id=project_id,
        proxy_url=getattr(credential, "proxy_url", None),
    )


async def test_credential(credential: SSHCredential) -> tuple[bool, str]:
    """凭据连通性验证：连接 + ``echo ok``（固定模板）。返回 (ok, detail)。"""
    try:
        private_key = decrypt_secret(credential.private_key_encrypted)
        passphrase = (
            decrypt_secret(credential.passphrase_encrypted)
            if credential.passphrase_encrypted
            else None
        )
        session = await get_connector().connect(
            host=credential.host,
            port=credential.port,
            username=credential.username,
            private_key=private_key,
            passphrase=passphrase,
        )
    except Exception as e:  # noqa: BLE001 — 连接失败要转成 {ok: false} 而非 500
        return False, f"{type(e).__name__}: {e}"
    try:
        result = await session.run("echo ok", timeout=DEFAULT_CMD_TIMEOUT_SECONDS)
        if result.exit_status == 0 and "ok" in result.stdout:
            return True, "ok"
        return False, f"echo 测试失败：exit={result.exit_status} stderr={result.stderr[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    finally:
        await session.close()


def parse_loadavg_block(stdout: str) -> dict[str, Any]:
    """解析 `nproc; cat /proc/loadavg` 的合并输出 → {cores, load_1m/5m/15m}。容错，缺项跳过。"""
    cpu: dict[str, Any] = {}
    lines = [ln.strip() for ln in stdout.splitlines() if ln.strip()]
    for ln in lines:
        if ln.isdigit() and "cores" not in cpu:
            cpu["cores"] = int(ln)
            continue
        parts = ln.split()
        if len(parts) >= 3 and "load_1m" not in cpu:
            try:
                cpu["load_1m"], cpu["load_5m"], cpu["load_15m"] = (
                    float(parts[0]),
                    float(parts[1]),
                    float(parts[2]),
                )
            except ValueError:
                continue
    return cpu


def parse_free_mem(stdout: str) -> dict[str, int]:
    """解析 `free -m` 的 Mem 行 → {total_mib, used_mib, available_mib}。容错。"""
    for ln in stdout.splitlines():
        parts = ln.split()
        if len(parts) >= 3 and parts[0].rstrip(":").lower() == "mem":
            try:
                mem = {"total_mib": int(parts[1]), "used_mib": int(parts[2])}
                if len(parts) >= 7:
                    mem["available_mib"] = int(parts[6])
                return mem
            except ValueError:
                return {}
    return {}


def parse_df(stdout: str, max_mounts: int = 8) -> list[dict[str, Any]]:
    """解析 `df -PB1M`（无表头）行 → [{mount, total_mib, used_mib, avail_mib}]。容错。"""
    disks: list[dict[str, Any]] = []
    for ln in stdout.splitlines():
        parts = ln.split()
        if len(parts) < 6:
            continue
        try:
            disks.append(
                {
                    "mount": parts[5],
                    "total_mib": int(parts[1].rstrip("M")),
                    "used_mib": int(parts[2].rstrip("M")),
                    "avail_mib": int(parts[3].rstrip("M")),
                }
            )
        except ValueError:
            continue
        if len(disks) >= max_mounts:
            break
    return disks


async def probe_sysinfo(credential: SSHCredential) -> dict[str, Any]:
    """服务器系统状态一览（CPU/内存/磁盘/GPU），设置页展示用。

    固定模板命令 + 容错解析（确定性探测，非 LLM）；连接失败 → {ok: False, detail}；
    单项探测失败该项缺省（尽力而为，不因一项失败整体报错）。
    """
    try:
        private_key = decrypt_secret(credential.private_key_encrypted)
        passphrase = (
            decrypt_secret(credential.passphrase_encrypted)
            if credential.passphrase_encrypted
            else None
        )
        session = await get_connector().connect(
            host=credential.host,
            port=credential.port,
            username=credential.username,
            private_key=private_key,
            passphrase=passphrase,
        )
    except Exception as e:  # noqa: BLE001 — 连接失败转 {ok: false} 而非 500
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    info: dict[str, Any] = {"ok": True, "host": credential.host}
    probes = (
        ("cpu", "nproc 2>/dev/null; cat /proc/loadavg 2>/dev/null", parse_loadavg_block),
        ("mem", "free -m 2>/dev/null", parse_free_mem),
        (
            "disks",
            "df -PB1M -x tmpfs -x devtmpfs -x overlay -x squashfs 2>/dev/null | tail -n +2",
            parse_df,
        ),
        (
            "gpus",
            "nvidia-smi --query-gpu=index,memory.total,memory.free "
            "--format=csv,noheader,nounits 2>/dev/null",
            parse_gpu_csv,
        ),
    )
    try:
        for key, command, parse in probes:
            try:
                result = await session.run(command, timeout=DEFAULT_CMD_TIMEOUT_SECONDS)
            except Exception:  # noqa: BLE001 — 单项失败跳过
                continue
            if result.exit_status == 0:
                info[key] = parse(result.stdout)
    finally:
        await session.close()
    return info


class SSHExecutor:
    """绑定单个实验工作目录的白名单命令执行器（全部远程命令过审计）。"""

    def __init__(
        self,
        session: SSHSession,
        *,
        exp_id: str,
        host: str,
        project_id: uuid.UUID,
        actor: str = "agent:experiment",
        proxy_url: str | None = None,
    ) -> None:
        self._session = session
        self.exp_id = validate_exp_id(exp_id)
        self.host = host
        self.project_id = project_id
        self.actor = actor
        self.proxy_url = validate_proxy_url(proxy_url)

    def _proxy_prefix(self) -> str:
        """setup 阶段（env.sh 尚未写入/不 source）需要的代理导出前缀。"""
        if not self.proxy_url:
            return ""
        u = self.proxy_url
        return (
            f"export http_proxy={u} https_proxy={u} HTTP_PROXY={u} HTTPS_PROXY={u} "
            "no_proxy=localhost,127.0.0.1; "
        )

    @property
    def workdir(self) -> str:
        return f"{WORKDIR_ROOT_SHELL}/{self.exp_id}"

    @property
    def _sftp_dir(self) -> str:
        return f"{WORKDIR_ROOT_SFTP}/{self.exp_id}"

    async def close(self) -> None:
        await self._session.close()

    # ---- 审计 ----

    async def _audit(self, command: str) -> None:
        logger.info("ssh.exec host=%s exp=%s cmd=%s", self.host, self.exp_id, command)
        async with get_sessionmaker()() as session:
            session.add(
                Activity(
                    project_id=self.project_id,
                    actor=self.actor,
                    kind="ssh.exec",
                    message=f"SSH 执行：{command[:200]}",
                    payload={"host": self.host, "experiment_id": self.exp_id, "command": command},
                )
            )
            await session.commit()

    async def _run(
        self, command: str, timeout: float | None = DEFAULT_CMD_TIMEOUT_SECONDS
    ) -> SSHResult:
        await self._audit(command)
        return await self._session.run(command, timeout=timeout)

    # ---- 白名单命令模板（唯一的远程命令来源） ----

    async def mkdir_workdir(self) -> SSHResult:
        return await self._run(f"mkdir -p {self.workdir}")

    async def write_files(self, files: dict[str, str]) -> list[str]:
        """SFTP 写文件到 workdir（LLM 产出的内容只经此通道落盘，不进 shell）。"""
        written: list[str] = []
        for name, content in files.items():
            rel = _validate_relpath(name)
            parent = posixpath.dirname(rel)
            if parent:
                await self._run(f"mkdir -p {self.workdir}/{parent}")
            path = f"{self._sftp_dir}/{rel}"
            await self._audit(f"sftp:write {self.workdir}/{rel} ({len(content)} bytes)")
            await self._session.write_file(path, str(content))
            written.append(rel)
        return written

    async def setup_venv(self, timeout: float = SETUP_TIMEOUT_SECONDS) -> SSHResult:
        # Ubuntu 常见缺 python3-venv（无 ensurepip）→ 降级：pip3 --user 装 virtualenv
        # （自带 pip，不依赖 ensurepip）再建环境。2026-07-15 实验室 GPU 服务器实测。
        from app.core.config import get_settings

        index = get_settings().pip_index_url
        index_arg = f" -i {index}" if index else ""
        return await self._run(
            f"cd {self.workdir} && {self._proxy_prefix()}"
            "{ python3 -m venv .venv 2>/dev/null && test -x .venv/bin/pip; } || "
            "{ rm -rf .venv && pip3 install --user -q virtualenv"
            f"{index_arg} && python3 -m virtualenv -q .venv; }} && "
            # 激活而非直调 .venv/bin/pip：部分包的构建脚本调用裸 `python`，
            # 激活后 PATH 里才有（如 fast-downward-textworld，2026-07-15 实测）
            f". .venv/bin/activate && pip install{index_arg} -r requirements.txt",
            timeout=timeout,
        )

    async def run_smoke(self, timeout: float = SMOKE_TIMEOUT_SECONDS) -> SSHResult:
        # {} 分组保证 cd 失败时不在错误目录执行；组退出码即 run.sh 退出码
        return await self._run(
            f"cd {self.workdir} && {{ {ENV_SOURCE_PREFIX} bash run.sh --smoke; }}",
            timeout=timeout,
        )

    async def run_plot(self, timeout: float = PLOT_TIMEOUT_SECONDS) -> SSHResult:
        """执行绘图脚本（固定文件名，LLM 只产出脚本内容，docs/api-m5-a.md §1）。"""
        return await self._run(
            f"cd {self.workdir} && {{ {ENV_SOURCE_PREFIX} .venv/bin/python plot_figures.py; }}",
            timeout=timeout,
        )

    async def ensure_plot_deps(self, timeout: float = SETUP_TIMEOUT_SECONDS) -> SSHResult:
        """绘图前确定性保证 matplotlib 可用（幂等，已装秒过）。

        绘图是平台自己的模板（plot_figures.py→PNG），依赖属平台责任——缺包不该指望
        figures 修复循环（它只能重写脚本、装不了包，实测缺 matplotlib 时修 N 次必降级 0 图）。
        env.sh 会激活 venv（裸机），故裸 python/pip 即目标环境。"""
        from app.core.config import get_settings

        index = get_settings().pip_index_url
        index_arg = f" -i {index}" if index else ""
        return await self._run(
            f"cd {self.workdir} && {{ {ENV_SOURCE_PREFIX} "
            f'python -c "import matplotlib" 2>/dev/null || '
            f"{{ {self._proxy_prefix()}pip install{index_arg} matplotlib; }}; }}",
            timeout=timeout,
        )

    async def list_dir(self, subdir: str) -> list[str]:
        """列 workdir 子目录内文件名（相对路径过白名单校验；目录缺失返回空）。"""
        rel = _validate_relpath(subdir)
        result = await self._run(f"ls -1 {self.workdir}/{rel} 2>/dev/null")
        if result.exit_status != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    async def list_tree(self, max_files: int = 300) -> list[dict[str, Any]]:
        """列 workdir 内全部代码/产物文件（相对路径 + 字节大小），供前端代码浏览。

        固定模板命令：剪掉 .venv/__pycache__/data_cache/.git 等重目录，限深限量。
        失败/空 → 空列表（上层回退 checkpoint 快照）。ContainerRunner 继承（host 侧读）。
        """
        result = await self._run(
            f"cd {self.workdir} && find . -maxdepth 4 "
            "\\( -name .venv -o -name __pycache__ -o -name data_cache -o -name .git \\) "
            f"-prune -o -type f -printf '%s %P\\n' 2>/dev/null | head -{int(max_files)}"
        )
        if result.exit_status != 0:
            return []
        files: list[dict[str, Any]] = []
        for line in result.stdout.splitlines():
            size_s, _, path = line.strip().partition(" ")
            if not path or not size_s.isdigit():
                continue
            files.append({"path": path, "size": int(size_s)})
        return sorted(files, key=lambda f: str(f["path"]))

    async def read_file(self, relpath: str) -> bytes:
        """SFTP 读回 workdir 内文件（figures 拉回本地镜像用）。"""
        rel = _validate_relpath(relpath)
        await self._audit(f"sftp:read {self.workdir}/{rel}")
        return await self._session.read_file(f"{self._sftp_dir}/{rel}")

    async def read_metrics_json(self) -> str | None:
        """读 workdir/metrics.json（可选：训练脚本可能写的指标文件）；缺失返回 None。"""
        result = await self._run(f"cat {self.workdir}/metrics.json 2>/dev/null")
        text = result.stdout.strip()
        if result.exit_status != 0 or not text:
            return None
        return text

    async def launch_run(self) -> tuple[int, str]:
        """后台启动正式运行，返回 (PID, 启动命令)。退出码落 run.exit 供轮询读取。"""
        # PYTHONUNBUFFERED=1 + stdbuf 行缓冲：让 run.log 实时刷新，轮询才能镜像日志/解析指标
        # （否则 Python 块缓冲 stdout，长实验期间 run.log 长时间为空，无法观测进度）。
        command = (
            f"cd {self.workdir} && rm -f run.exit && "
            f"nohup bash -c 'export PYTHONUNBUFFERED=1; {ENV_SOURCE_PREFIX} "
            f"stdbuf -oL -eL bash run.sh > run.log 2>&1; "
            "echo $? > run.exit' >/dev/null 2>&1 & echo $!"
        )
        result = await self._run(command)
        try:
            pid = int(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as e:
            raise SSHExecError(f"launch_run 未返回 PID：{result.stdout!r}") from e
        return pid, command

    async def check_pid(self, pid: int) -> bool:
        result = await self._run(f"kill -0 {int(pid)} 2>/dev/null && echo alive || echo dead")
        return "alive" in result.stdout

    async def read_exit_code(self) -> int | None:
        result = await self._run(f"cat {self.workdir}/run.exit 2>/dev/null")
        text = result.stdout.strip()
        if result.exit_status != 0 or not text:
            return None
        try:
            return int(text.splitlines()[-1])
        except ValueError:
            return None

    async def tail_log(self, offset: int = 0) -> tuple[str, int]:
        """增量读 run.log：返回 (offset 之后的新内容, 新 offset)。"""
        offset = max(0, int(offset))
        result = await self._run(f"tail -c +{offset + 1} {self.workdir}/run.log 2>/dev/null")
        chunk = result.stdout if result.exit_status == 0 else ""
        return chunk, offset + len(chunk.encode("utf-8"))

    async def kill_pid(self, pid: int) -> SSHResult:
        return await self._run(f"kill {int(pid)} 2>/dev/null || true")

    def _install_script(self) -> str:
        """依赖安装脚本正文（venv + pip install）——launch_setup 后台执行这段，逻辑同 setup_venv。"""
        from app.core.config import get_settings

        index = get_settings().pip_index_url
        index_arg = f" -i {index}" if index else ""
        return (
            f"{self._proxy_prefix()}"
            "{ python3 -m venv .venv 2>/dev/null && test -x .venv/bin/pip; } || "
            f"{{ rm -rf .venv && pip3 install --user -q virtualenv{index_arg} && "
            "python3 -m virtualenv -q .venv; } && "
            f". .venv/bin/activate && pip install{index_arg} -r requirements.txt"
        )

    async def launch_setup(self) -> tuple[int, str]:
        """后台启动依赖安装：退出码落 setup.exit、日志落 setup.log，返回 (PID, 命令)。
        经 nohup 脱离会话——轮询期间 SSH 瞬时断连可**重连后接着跟同一安装进程**，而非从头重装。"""
        command = (
            f"cd {self.workdir} && rm -f setup.exit && "
            f"nohup bash -c 'export PYTHONUNBUFFERED=1; {{ {self._install_script()} ; }} "
            "> setup.log 2>&1; echo $? > setup.exit' >/dev/null 2>&1 & echo $!"
        )
        result = await self._run(command)
        try:
            pid = int(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as e:
            raise SSHExecError(f"launch_setup 未返回 PID：{result.stdout!r}") from e
        return pid, command

    async def read_setup_exit(self) -> int | None:
        result = await self._run(f"cat {self.workdir}/setup.exit 2>/dev/null")
        text = result.stdout.strip()
        if result.exit_status != 0 or not text:
            return None
        try:
            return int(text.splitlines()[-1])
        except ValueError:
            return None

    async def read_setup_log(self, tail_chars: int = 2000) -> str:
        """读依赖安装日志尾部（失败时把报错回给 LLM 修 requirements/run.sh）。"""
        result = await self._run(f"tail -c {int(tail_chars)} {self.workdir}/setup.log 2>/dev/null")
        return result.stdout if result.exit_status == 0 else ""

    async def probe_gpu(self) -> list[dict[str, int]]:
        """资源预检：探测本机 GPU（每卡 index/显存总量/空闲，MiB）。

        无 nvidia-smi（无 GPU/驱动）或命令失败 → 返回空列表（= 本机无可用 GPU）。
        确定性探测（普通命令），不涉及判断，故走这里而非 LLM。
        """
        result = await self._run(
            "nvidia-smi --query-gpu=index,memory.total,memory.free "
            "--format=csv,noheader,nounits 2>/dev/null"
        )
        if result.exit_status != 0:
            return []
        return parse_gpu_csv(result.stdout)

    async def host_path_exists(self, path: str) -> bool:
        """资源预检：主机上某绝对/家目录路径是否存在（模型目录/数据集文件等）。"""
        safe = validate_host_path(path)
        result = await self._run(f"test -e {safe} && echo yes || echo no")
        return "yes" in result.stdout

    async def read_host_file(self, path: str) -> str | None:
        """资源预检：读主机上某文件内容（如模型 config.json），缺失/失败 → None。"""
        safe = validate_host_path(path)
        result = await self._run(f"cat {safe} 2>/dev/null")
        if result.exit_status != 0:
            return None
        return result.stdout
