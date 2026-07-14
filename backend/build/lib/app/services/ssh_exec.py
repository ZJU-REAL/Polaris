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


class SSHExecError(Exception):
    """SSH 执行层错误基类。"""


class SSHPathViolationError(SSHExecError):
    """路径越界：目标不在 ~/polaris_runs/<exp_id> 之下。"""


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
    return SSHExecutor(session, exp_id=exp_id, host=credential.host, project_id=project_id)


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
    ) -> None:
        self._session = session
        self.exp_id = validate_exp_id(exp_id)
        self.host = host
        self.project_id = project_id
        self.actor = actor

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
        return await self._run(
            f"cd {self.workdir} && python3 -m venv .venv && "
            ".venv/bin/pip install -r requirements.txt",
            timeout=timeout,
        )

    async def run_smoke(self, timeout: float = SMOKE_TIMEOUT_SECONDS) -> SSHResult:
        return await self._run(f"cd {self.workdir} && bash run.sh --smoke", timeout=timeout)

    async def run_plot(self, timeout: float = PLOT_TIMEOUT_SECONDS) -> SSHResult:
        """执行绘图脚本（固定文件名，LLM 只产出脚本内容，docs/api-m5-a.md §1）。"""
        return await self._run(
            f"cd {self.workdir} && .venv/bin/python plot_figures.py", timeout=timeout
        )

    async def list_dir(self, subdir: str) -> list[str]:
        """列 workdir 子目录内文件名（相对路径过白名单校验；目录缺失返回空）。"""
        rel = _validate_relpath(subdir)
        result = await self._run(f"ls -1 {self.workdir}/{rel} 2>/dev/null")
        if result.exit_status != 0:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

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
        command = (
            f"cd {self.workdir} && rm -f run.exit && "
            "nohup bash -c 'bash run.sh > run.log 2>&1; echo $? > run.exit' "
            ">/dev/null 2>&1 & echo $!"
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
