"""内存 fake SSH（实现 app/services/ssh_exec 的 SSHConnector/SSHSession 协议）。

- 记录全部命令与 SFTP 写入，可编程冒烟退出码序列 / 运行日志 / 退出码 / 存活行为；
- ``on_command`` 钩子可在特定命令时机注入副作用（如把 voyage 置 cancelled）。
"""

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.services.ssh_exec import SSHResult


@dataclass
class FakeSSHServer:
    commands: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    connects: list[tuple[str, int, str]] = field(default_factory=list)
    killed: list[int] = field(default_factory=list)

    connect_error: str | None = None  # 置为字符串则 connect 抛 ConnectionError
    venv_exit: int = 0
    smoke_exits: list[int] = field(default_factory=list)  # 逐次弹出；耗尽后恒 0
    smoke_stderr: str = "Traceback (most recent call last): boom"
    run_log: str = ""
    run_exit: int | None = 0  # None = 进程一直不结束（cat run.exit 读不到）
    pid: int = 4242
    launched: bool = False
    # 命令钩子：async (command) -> None，在记录命令后调用
    on_command: Callable[[str], Awaitable[None]] | None = None


class FakeSSHSession:
    def __init__(self, server: FakeSSHServer) -> None:
        self._server = server

    async def run(self, command: str, timeout: float | None = None) -> SSHResult:
        server = self._server
        server.commands.append(command)
        if server.on_command is not None:
            await server.on_command(command)

        if "--smoke" in command:
            exit_code = server.smoke_exits.pop(0) if server.smoke_exits else 0
            if exit_code == 0:
                return SSHResult(0, "smoke ok\n", "")
            return SSHResult(exit_code, "", server.smoke_stderr)
        if "echo $!" in command:  # launch_run
            server.launched = True
            return SSHResult(0, f"{server.pid}\n", "")
        if "kill -0" in command:  # check_pid：run_exit 已就绪则进程视为已退出
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
        if command.startswith("kill "):
            m = re.search(r"kill (\d+)", command)
            if m:
                server.killed.append(int(m.group(1)))
            return SSHResult(0, "", "")
        if "venv" in command or "pip install" in command:
            return SSHResult(server.venv_exit, "", "pip failed" if server.venv_exit else "")
        if command.startswith("mkdir -p"):
            return SSHResult(0, "", "")
        if "echo ok" in command:
            return SSHResult(0, "ok\n", "")
        return SSHResult(0, "", "")

    async def write_file(self, path: str, content: str) -> None:
        self._server.files[path] = content

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
