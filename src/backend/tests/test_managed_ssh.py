from app.services.managed_commands import (
    OperationContext,
    RepairScope,
    failure_from_exception,
)
from app.services.managed_ssh import ManagedCommandLaunchError, SSHManagedCommands
from app.services.ssh_exec import SSHResult
from tests.fake_ssh import FakeSSHServer, FakeSSHSession


def _manager(server: FakeSSHServer) -> SSHManagedCommands:
    session = FakeSSHSession(server)
    return SSHManagedCommands(
        session=session,
        run=session.run,
        shell_workdir="~/polaris_runs/test",
        sftp_workdir="polaris_runs/test",
    )


def _context() -> OperationContext:
    return OperationContext(
        phase="application.run",
        operation="experiment-run",
        display_command="bash run.sh",
        target="host",
        repair_scope=RepairScope.APPLICATION_FILES,
    )


async def test_concurrent_logical_start_attaches_active_attempt():
    server = FakeSSHServer(run_exit=None)
    manager = _manager(server)

    first = await manager.start(_context(), "bash run.sh")
    second = await manager.start(_context(), "bash run.sh")

    assert second.attempt_id == first.attempt_id
    assert second.process_id == first.process_id
    assert sum("nohup setsid bash" in command for command in server.commands) == 1
    pointer_index = next(
        i for i, command in enumerate(server.commands) if "/current.tmp" in command
    )
    launch_index = next(
        i for i, command in enumerate(server.commands) if "nohup setsid bash" in command
    )
    assert pointer_index < launch_index
    assert "&& nohup" not in server.commands[launch_index]


async def test_completed_attempt_allows_a_new_attempt():
    server = FakeSSHServer(run_exit=0)
    manager = _manager(server)

    first = await manager.start(_context(), "bash run.sh")
    second = await manager.start(_context(), "bash run.sh")

    assert second.attempt_id != first.attempt_id
    assert sum("nohup setsid bash" in command for command in server.commands) == 2


async def test_recover_current_returns_completed_attempt_without_relaunching():
    server = FakeSSHServer(run_exit=0)
    manager = _manager(server)

    launched = await manager.start(_context(), "bash run.sh")
    recovered = await manager.recover_current(_context())

    assert recovered is not None
    assert recovered.attempt_id == launched.attempt_id
    assert recovered.process_id == launched.process_id
    snapshot = await manager.snapshot(recovered)
    assert snapshot.exit_status == 0
    assert sum("nohup setsid bash" in command for command in server.commands) == 1


async def test_stop_only_targets_current_attempt_and_verifies_exit():
    server = FakeSSHServer(run_exit=None)
    manager = _manager(server)
    handle = await manager.start(_context(), "bash run.sh")

    outcome = await manager.stop(handle)
    assert outcome.confirmed is True
    assert outcome.status == "stopped"
    assert handle.process_group_id in server.killed
    snapshot = await manager.snapshot(handle)
    assert snapshot.process_alive is False
    assert snapshot.exit_status == -15


async def test_stop_refuses_an_attempt_replaced_before_the_signal():
    """The current-attempt check must live in the same remote critical section as kill."""
    server = FakeSSHServer(run_exit=None)
    manager = _manager(server)
    handle = await manager.start(_context(), "bash run.sh")
    operation_dir = manager._operation_dir(handle.operation_id)
    server.managed_files[f"{operation_dir}/current"] = (
        "22222222-2222-2222-2222-222222222222"
    )

    outcome = await manager.stop(handle)
    assert outcome.confirmed is False
    assert outcome.status == "attempt_changed"
    assert server.killed == []
    stop_commands = [c for c in server.commands if "# polaris-managed-stop" in c]
    assert len(stop_commands) == 1


async def test_stop_refuses_changed_process_identity():
    server = FakeSSHServer(run_exit=None)
    manager = _manager(server)
    handle = await manager.start(_context(), "bash run.sh")
    prefix = server.managed_prefix_by_pid[handle.process_id]
    server.managed_files[f"{prefix}.pid"] = str(handle.process_id + 1)

    outcome = await manager.stop(handle)
    assert outcome.confirmed is False
    assert outcome.status == "identity_changed"
    assert server.killed == []


async def test_stop_reports_when_the_attempt_already_exited():
    server = FakeSSHServer(run_exit=0)
    manager = _manager(server)
    handle = await manager.start(_context(), "bash run.sh")

    outcome = await manager.stop(handle)

    assert outcome.confirmed is True
    assert outcome.status == "already_exited"
    assert server.killed == []


async def test_launch_timeout_recovers_the_durable_attempt():
    server = FakeSSHServer(run_exit=None)
    session = FakeSSHSession(server)

    class LaunchTimeout(TimeoutError):
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.exit_status = 0
            super().__init__("launch channel timed out after returning the PID")

    async def run(command: str, timeout: float | None = None):
        result = await session.run(command, timeout)
        if "nohup setsid bash" in command:
            raise LaunchTimeout(result.stdout)
        return result

    manager = SSHManagedCommands(
        session=session,
        run=run,
        shell_workdir="~/polaris_runs/test",
        sftp_workdir="polaris_runs/test",
    )
    handle = await manager.start(_context(), "bash run.sh")

    assert handle.process_id == server.pid
    assert await manager.current_attempt_id(handle.operation_id) == handle.attempt_id
    assert sum("nohup setsid bash" in command for command in server.commands) == 1


def test_launch_error_preserves_operation_context():
    context = _context()
    error = ManagedCommandLaunchError(context, "bash run.sh", TimeoutError())
    assert error.operation_context is context
    assert error.command == "bash run.sh"
    report = failure_from_exception(error)
    assert report.phase == "application.run"
    assert report.operation == "experiment-run"
    assert report.repair_scope == RepairScope.APPLICATION_FILES


# ---- 输出增量读取 / 启动脚本 / pid 读取的加固 ----


async def test_incremental_read_counts_bytes_not_characters():
    """偏移量是字节口径，回来的却是解码后的 str。

    直接按 len(text.encode()) 记账，在多字节字符上会与真实消费的字节数错位，
    下一轮从错误位置续读——中文日志和带 unicode 的进度条都会踩到。
    """
    server = FakeSSHServer(run_exit=None)
    manager = _manager(server)
    handle = await manager.start(_context(), "bash run.sh")
    prefix = server.managed_prefix_by_pid[handle.process_id]
    text = "训练开始 ✅\n第 1 轮 loss=0.5\n"
    server.managed_files[f"{prefix}.stdout"] = text

    chunks, next_stdout, _ = await manager.read_output(handle)
    assert [c.text for c in chunks if c.stream == "stdout"] == [text]
    # 续读位置必须落在字节末尾，而不是字符数
    assert next_stdout == len(text.encode())

    # 从上次的偏移接着读：没有新内容就该是空，且偏移不动
    server.managed_files[f"{prefix}.stdout"] = text + "第 2 轮\n"
    chunks, after, _ = await manager.read_output(handle, stdout_offset=next_stdout)
    assert [c.text for c in chunks if c.stream == "stdout"] == ["第 2 轮\n"]
    assert after == len((text + "第 2 轮\n").encode())


async def test_incremental_read_is_capped_per_round():
    """远端两次轮询之间刷了巨量日志时，单轮只取上限，剩下的下一轮再来。"""
    from app.services.managed_ssh import _MAX_OUTPUT_CHUNK_BYTES

    server = FakeSSHServer(run_exit=None)
    manager = _manager(server)
    handle = await manager.start(_context(), "bash run.sh")
    prefix = server.managed_prefix_by_pid[handle.process_id]
    server.managed_files[f"{prefix}.stdout"] = "x" * (_MAX_OUTPUT_CHUNK_BYTES * 2)

    chunks, next_stdout, _ = await manager.read_output(handle)
    body = "".join(c.text for c in chunks if c.stream == "stdout")
    assert len(body) == _MAX_OUTPUT_CHUNK_BYTES
    assert next_stdout == _MAX_OUTPUT_CHUNK_BYTES


async def test_launcher_survives_a_command_ending_in_a_comment():
    """`{ cmd; }` 单行包裹时，以注释收尾的命令会把 `; }` 一起注释掉。

    那样整个启动脚本是 bash 语法错误，任务起不来还看不出原因。
    """
    server = FakeSSHServer(run_exit=None)
    manager = _manager(server)
    await manager.start(_context(), "bash run.sh  # 跑主实验")

    launcher = next(c for c in server.commands if c.startswith("#!/usr/bin/env bash"))
    assert "bash run.sh  # 跑主实验\n" in launcher
    # 收尾的花括号必须独占一行，不能跟在被注释的那行后面
    assert "\n} >${prefix}.stdout 2>${prefix}.stderr\n" in launcher


async def test_corrupt_pid_file_does_not_become_a_stray_kill():
    """pid/pgid 会拼进 `kill -- -<pgid>`。文件写坏时读出 0/负数不能当成有效值。"""
    server = FakeSSHServer(run_exit=None)
    manager = _manager(server)
    handle = await manager.start(_context(), "bash run.sh")
    prefix = server.managed_prefix_by_pid[handle.process_id]
    server.managed_files[f"{prefix}.pid"] = "0"
    server.managed_files[f"{prefix}.pgid"] = "-1"

    snapshot = await manager.snapshot(handle)
    # 读不到可信值时回退到 handle 上的原值，而不是把 0 / -1 传下去
    assert snapshot.process_id == handle.process_id
    assert snapshot.process_group_id == handle.process_group_id


def test_terminal_log_is_redacted_before_it_reaches_disk(tmp_path, monkeypatch):
    """终端日志经 /experiments/{id}/terminal-logs 原样发给成员与管理员。

    快照和失败报告都脱敏了，这个最大的外露面不能例外——实验脚本 echo 一次
    HF_TOKEN 就会进所有人的终端面板。
    """
    from app.core.config import get_settings
    from app.services import experiments as experiments_service

    settings = get_settings()
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    experiments_service.append_terminal_output(
        "11111111-1111-1111-1111-111111111111",
        operation="experiment-run",
        stream="stdout",
        text="export HF_TOKEN=hf_cccccccccccccccccccccccccccccc\nepoch 1 ok\n",
    )
    content = experiments_service.terminal_log_path(
        "11111111-1111-1111-1111-111111111111"
    ).read_text()
    assert "hf_cccccccccccccccccccccccccccccc" not in content
    assert "[REDACTED]" in content
    assert "epoch 1 ok" in content  # 正常输出不受影响


def test_run_log_is_redacted_before_it_reaches_disk(tmp_path, monkeypatch):
    from app.core.config import get_settings
    from app.services import experiments as experiments_service

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
    path = experiments_service.append_local_log(
        "11111111-1111-1111-1111-111111111111",
        1,
        "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz\nloss=0.2\n",
    )
    content = path.read_text()
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in content
    assert "[REDACTED]" in content
    assert "loss=0.2" in content


async def test_gpu_usage_is_attributed_to_the_managed_process_group_only():
    server = FakeSSHServer(run_exit=None)
    manager = _manager(server)
    handle = await manager.start(_context(), "bash run.sh")
    original_run = manager._run

    async def run(command: str, timeout: float | None = None):
        if command.startswith("ps -eo pid="):
            return SSHResult(
                0,
                f"{handle.process_id} 1 {handle.process_group_id}\n"
                f"8001 {handle.process_id} 9000\n"
                "9001 1 9001\n",
                "",
            )
        if command.startswith("nvidia-smi --query-compute-apps"):
            return SSHResult(0, "8001, 4096\n9001, 8192\n", "")
        return await original_run(command, timeout)

    manager._run = run
    usage = await manager.gpu_usage(handle)

    assert usage.status == "active"
    assert usage.process_ids == (8001,)
    assert usage.used_memory_mib == 4096


async def test_gpu_usage_does_not_claim_an_unrelated_machine_process():
    server = FakeSSHServer(run_exit=None)
    manager = _manager(server)
    handle = await manager.start(_context(), "bash run.sh")
    original_run = manager._run

    async def run(command: str, timeout: float | None = None):
        if command.startswith("ps -eo pid="):
            return SSHResult(0, f"{handle.process_id} 1 {handle.process_group_id}\n", "")
        if command.startswith("nvidia-smi --query-compute-apps"):
            return SSHResult(0, "9001, 8192\n", "")
        return await original_run(command, timeout)

    manager._run = run
    usage = await manager.gpu_usage(handle)

    assert usage.status == "idle"
    assert usage.used_memory_mib == 0
