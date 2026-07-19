"""Runner 抽象的单元测试：现有 SSH 执行器就是 RemoteHostRunner，且满足 kind 无关的 Runner 接口。"""

from app.agents.voyage import runner
from app.services import ssh_exec

# Runner 接口应覆盖的 kind 无关原语（实验循环只依赖这些）。
_PRIMITIVES = (
    "workdir",
    "mkdir_workdir",
    "write_files",
    "read_file",
    "list_dir",
    "read_metrics_json",
    "setup_venv",
    "run_smoke",
    "run_plot",
    "launch_run",
    "check_pid",
    "read_exit_code",
    "tail_log",
    "kill_pid",
    "close",
)


def test_remote_host_runner_is_current_ssh_executor():
    """现有行为 = RemoteHostRunner（SSH 主机上的裸机 venv 执行）——零行为变化的解耦。"""
    assert runner.RemoteHostRunner is ssh_exec.SSHExecutor


def test_runner_protocol_covers_primitives():
    for name in _PRIMITIVES:
        assert hasattr(runner.Runner, name), f"Runner 协议缺原语：{name}"


def test_ssh_executor_satisfies_runner():
    """现有 SSHExecutor 结构上满足 Runner（未来 Container/Api Runner 挂同一接口）。"""
    for name in _PRIMITIVES:
        assert hasattr(ssh_exec.SSHExecutor, name), f"SSHExecutor 缺原语：{name}"
