"""内存 fake SSH（实现 app/services/ssh_exec 的 SSHConnector/SSHSession 协议）。

- 记录全部命令与 SFTP 写入，可编程冒烟退出码序列 / 运行日志 / 退出码 / 存活行为；
- 迭代（M5-A）：``run_logs`` / ``run_exits`` 逐轮弹出（launch 时生效），耗尽后
  回落到 ``run_log`` / ``run_exit``；``plot_outputs`` 在执行 plot_figures.py 时
  写入远端 files 模拟脚本产图；``metrics_json`` 模拟可选 workdir/metrics.json；
- ``on_command`` 钩子可在特定命令时机注入副作用（如把 voyage 置 cancelled）。
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.services.ssh_exec import SSHResult


@dataclass
class FakeSSHServer:
    commands: list[str] = field(default_factory=list)
    files: dict[str, str | bytes] = field(default_factory=dict)
    connects: list[tuple[str, int, str]] = field(default_factory=list)
    killed: list[int] = field(default_factory=list)

    connect_error: str | None = None  # 置为字符串则 connect 抛 ConnectionError
    venv_exit: int = 0
    venv_exits: list[int] = field(default_factory=list)  # 逐次弹出；耗尽后回落 venv_exit
    # launch_setup（后台脱离装依赖）用：setup.exit 内容 / 逐次弹出序列 / 日志 / 已启动标记
    setup_pid: int = 5252
    setup_exit: int | None = 0  # None = 安装进行中（setup.exit 尚未落盘）
    setup_exits: list[int | None] = field(
        default_factory=list
    )  # 逐次弹出（每次 launch_setup 生效）
    setup_log: str = "pip install log (fake)"
    setup_launched: bool = False
    # probe_gpu 用：每卡 (index, 显存总, 空闲) MiB；空列表 = 本机无 GPU/驱动
    gpus: list[tuple[int, int, int]] = field(default_factory=list)
    # 资源预检用：本机文件内容（cat <path> → 内容，如模型 config.json）；未登记 = 缺失
    host_files: dict[str, str] = field(default_factory=dict)
    host_paths: set[str] = field(default_factory=set)  # test -e <path> 存在的路径（数据集目录等）
    smoke_exits: list[int] = field(default_factory=list)  # 逐次弹出；耗尽后恒 0
    smoke_stderr: str = "Traceback (most recent call last): boom"
    run_log: str = ""
    run_exit: int | None = 0  # None = 进程一直不结束（cat run.exit 读不到）
    run_logs: list[str] = field(default_factory=list)  # 逐轮弹出（launch 生效）
    run_exits: list[int | None] = field(default_factory=list)  # 逐轮弹出（launch 生效）
    metrics_json: str | None = None  # 可选 workdir/metrics.json 内容（None = 文件缺失）
    plot_exits: list[int] = field(default_factory=list)  # 逐次弹出；耗尽后恒 0
    plot_stderr: str = "Traceback: plot boom"
    # plot_figures.py 执行成功时写入远端 figures/ 的产物（相对 workdir 路径 → 内容）
    plot_outputs: dict[str, str | bytes] = field(default_factory=dict)
    pid: int = 4242
    launched: bool = False
    # 命令钩子：async (command) -> None，在记录命令后调用
    on_command: Callable[[str], Awaitable[None]] | None = None


class FakeSSHSession:
    def __init__(self, server: FakeSSHServer) -> None:
        self._server = server

    @staticmethod
    def _sftp_dir(command: str) -> str | None:
        m = re.search(r"polaris_runs/([0-9a-f-]{36})", command)
        return f"polaris_runs/{m.group(1)}" if m else None

    async def run(self, command: str, timeout: float | None = None) -> SSHResult:
        server = self._server
        server.commands.append(command)
        if server.on_command is not None:
            await server.on_command(command)

        # —— 后台脱离装依赖（launch_setup + setup.exit/setup.log 轮询）；须在通用 launch/cat 之前 ——
        if ("setup.exit" in command or "_setup_container.sh" in command) and "echo $!" in command:
            server.setup_launched = True
            if server.setup_exits:
                server.setup_exit = server.setup_exits.pop(0)
            return SSHResult(0, f"{server.setup_pid}\n", "")
        if command.startswith("cat") and "setup.exit" in command:  # read_setup_exit
            if server.setup_launched and server.setup_exit is not None:
                return SSHResult(0, f"{server.setup_exit}\n", "")
            return SSHResult(1, "", "")  # 安装未完成 → 尚无退出码
        if "setup.log" in command:  # read_setup_log（tail -c N）
            return SSHResult(0, server.setup_log, "")

        if "--smoke" in command:
            exit_code = server.smoke_exits.pop(0) if server.smoke_exits else 0
            if exit_code == 0:
                return SSHResult(0, "smoke ok\n", "")
            return SSHResult(exit_code, "", server.smoke_stderr)
        if "plot_figures.py" in command and ".venv/bin/python" in command:  # run_plot
            exit_code = server.plot_exits.pop(0) if server.plot_exits else 0
            if exit_code != 0:
                return SSHResult(exit_code, "", server.plot_stderr)
            sftp_dir = self._sftp_dir(command)
            if sftp_dir:
                for rel, content in server.plot_outputs.items():
                    server.files[f"{sftp_dir}/{rel}"] = content
            return SSHResult(0, "", "")
        if "find ." in command and "-printf" in command:  # list_tree（代码浏览）
            sftp_dir = self._sftp_dir(command)
            if not sftp_dir:
                return SSHResult(1, "", "")
            prefix = f"{sftp_dir}/"
            pruned = (".venv", "__pycache__", "data_cache", ".git")
            lines = []
            for path, content in sorted(server.files.items()):
                if not path.startswith(prefix):
                    continue
                rel = path[len(prefix) :]
                if any(seg in pruned for seg in rel.split("/")):
                    continue
                size = len(content) if isinstance(content, bytes) else len(content.encode())
                lines.append(f"{size} {rel}")
            return SSHResult(0, "\n".join(lines) + ("\n" if lines else ""), "")
        if command.startswith("ls -1"):  # list_dir
            m = re.search(r"ls -1 ~/(\S+)", command)
            prefix = f"{m.group(1)}/" if m else None
            if not prefix:
                return SSHResult(1, "", "")
            names = sorted(
                path[len(prefix) :]
                for path in server.files
                if path.startswith(prefix) and "/" not in path[len(prefix) :]
            )
            if not names:
                return SSHResult(1, "", "")
            return SSHResult(0, "\n".join(names) + "\n", "")
        if "echo $!" in command:  # launch_run：逐轮弹出本轮日志/退出码
            server.launched = True
            if server.run_logs:
                server.run_log = server.run_logs.pop(0)
            if server.run_exits:
                server.run_exit = server.run_exits.pop(0)
            return SSHResult(0, f"{server.pid}\n", "")
        if "kill -0" in command:  # check_pid：exit 已就绪则进程视为已退出
            m = re.search(r"kill -0 (\d+)", command)
            q = int(m.group(1)) if m else -1
            if q == server.setup_pid:
                alive = server.setup_launched and server.setup_exit is None
            else:
                alive = server.launched and server.run_exit is None
            return SSHResult(0, "alive\n" if alive else "dead\n", "")
        if "tail -c" in command:
            if not server.launched:
                return SSHResult(1, "", "")
            m = re.search(r"tail -c \+(\d+)", command)
            offset = int(m.group(1)) - 1 if m else 0
            data = server.run_log.encode("utf-8")[offset:]
            return SSHResult(0, data.decode("utf-8"), "")
        if "run.exit" in command and command.startswith("cat"):
            if server.launched and server.run_exit is not None:
                return SSHResult(0, f"{server.run_exit}\n", "")
            return SSHResult(1, "", "")
        if "/metrics.json" in command and command.startswith("cat"):  # read_metrics_json
            if server.metrics_json is None:
                return SSHResult(1, "", "")
            return SSHResult(0, server.metrics_json, "")
        if command.startswith("kill "):
            m = re.search(r"kill (\d+)", command)
            if m:
                server.killed.append(int(m.group(1)))
            return SSHResult(0, "", "")
        if "nproc" in command:  # probe_sysinfo CPU（nproc + /proc/loadavg 合并输出）
            return SSHResult(0, "64\n1.25 0.80 0.50 2/2000 12345\n", "")
        if "free -m" in command:  # probe_sysinfo 内存
            return SSHResult(
                0,
                "              total        used        free      shared  buff/cache   available\n"
                "Mem:         515711       88320      120000        1024      307391      420000\n"
                "Swap:          8191           0        8191\n",
                "",
            )
        if "df -P" in command:  # probe_sysinfo 磁盘（-PB1M 无表头行）
            return SSHResult(
                0,
                "/dev/nvme0n1p2 1920000M 810000M 1010000M 45% /\n"
                "nfs:/data 9600000M 7200000M 2400000M 75% /data\n",
                "",
            )
        if "nvidia-smi" in command:  # probe_gpu：无卡 → 命令失败（模拟无 GPU/驱动）
            if not server.gpus:
                return SSHResult(127, "", "nvidia-smi: command not found")
            rows = "\n".join(f"{i}, {total}, {free}" for i, total, free in server.gpus)
            return SSHResult(0, rows + "\n", "")
        if command.startswith("test -e "):  # host_path_exists（资源预检数据集等）
            m = re.search(r"test -e (\S+)", command)
            path = m.group(1) if m else ""
            exists = path in server.host_paths or path in server.host_files
            return SSHResult(0, "yes\n" if exists else "no\n", "")
        if command.startswith("cat "):  # read_host_file（本机模型 config 等，非 workdir 文件）
            m = re.search(r"cat (\S+)", command)
            path = m.group(1) if m else ""
            if path and "polaris_runs" not in path:  # workdir 内 run.exit/metrics.json 交给下面处理
                if path in server.host_files:
                    return SSHResult(0, server.host_files[path], "")
                return SSHResult(1, "", "")  # 未登记 = 缺失
        if "import matplotlib" in command:  # ensure_plot_deps：默认依赖就绪
            return SSHResult(0, "", "")
        if "venv" in command or "pip install" in command:
            exit_code = server.venv_exits.pop(0) if server.venv_exits else server.venv_exit
            return SSHResult(exit_code, "", "pip failed" if exit_code else "")
        if command.startswith("mkdir -p"):
            return SSHResult(0, "", "")
        if "echo ok" in command:
            return SSHResult(0, "ok\n", "")
        return SSHResult(0, "", "")

    async def write_file(self, path: str, content: str) -> None:
        self._server.files[path] = content

    async def read_file(self, path: str) -> bytes:
        content = self._server.files.get(path)
        if content is None:
            raise FileNotFoundError(path)
        return content if isinstance(content, bytes) else content.encode("utf-8")

    async def close(self) -> None:
        pass


class FakeSSHConnector:
    def __init__(self, server: FakeSSHServer) -> None:
        self._server = server

    async def connect(
        self,
        *,
        host: str,
        port: int,
        username: str,
        private_key: str,
        passphrase: str | None = None,
    ) -> FakeSSHSession:
        if self._server.connect_error:
            raise ConnectionError(self._server.connect_error)
        assert "PRIVATE KEY" in private_key  # 凭据解密后才会到达连接层
        self._server.connects.append((host, port, username))
        return FakeSSHSession(self._server)
