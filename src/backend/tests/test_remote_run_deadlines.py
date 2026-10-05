"""Fixed remote deadlines and container child termination use no SSH/Docker process."""

import uuid
from types import SimpleNamespace

import pytest

from app.agents.voyage.runner import ContainerRunner, ContainerSpec
from app.services import experiments
from app.services.managed_commands import CommandSnapshot
from app.services.managed_ssh import ManagedCommandHandle, ManagedStopResult
from app.services.ssh_exec import SSHExecError, SSHExecutor, SSHResult


class MemorySession:
    def __init__(self):
        self.commands = []
        self.files = {}
        self.timeout_available = True
        self.setsid_available = True
        self.container_running = True
        self.container_exists = True
        self.inspect_available = True
        self.list_available = True
        self.stop_succeeds = True
        self.ledger_pgid = 77
        self.members = "77 S\n"
        self.members_available = True

    async def run(self, command, timeout=None):
        self.commands.append(command)
        if "command -v timeout" in command:
            return SSHResult(0 if self.timeout_available else 1, "", "")
        if "command -v setsid" in command:
            return SSHResult(0 if self.setsid_available else 1, "", "")
        if command.startswith("docker stop"):
            if self.stop_succeeds:
                self.container_running = False
            return SSHResult(0 if self.stop_succeeds else 1, "", "")
        if command.startswith("docker inspect"):
            if not self.inspect_available or not self.container_exists:
                return SSHResult(1, "", "unavailable")
            return SSHResult(0, "true\n" if self.container_running else "false\n", "")
        if command.startswith("docker container ls"):
            return SSHResult(0 if self.list_available else 1,
                             "container-id\n" if self.container_exists else "", "")
        if "cat " in command and ".polaris/container/" in command:
            return SSHResult(1, "", "missing") if self.ledger_pgid is None else SSHResult(
                0, str(self.ledger_pgid), ""
            )
        if "ps -eo pgid=,stat=" in command:
            return SSHResult(0 if self.members_available else 1, self.members, "")
        return SSHResult(0, "", "")

    async def write_file(self, path, content):
        self.files[path] = content


@pytest.fixture
def executors(monkeypatch):
    async def audit(*args, **kwargs):
        pass

    monkeypatch.setattr(SSHExecutor, "_audit", audit)
    session = MemorySession()
    fields = {"exp_id": str(uuid.uuid4()), "project_id": uuid.uuid4(), "host": "fixture"}
    host = SSHExecutor(session, **fields)
    container = ContainerRunner(session, **fields, spec=ContainerSpec(image="fixture:1"))
    captured = []

    async def start(context, command, *, attempt_id=None):
        handle = ManagedCommandHandle(
            context.operation, attempt_id or str(uuid.uuid4()), context, 4242, 4242
        )
        captured.append((handle, command))
        return handle

    monkeypatch.setattr(host, "start_managed_command", start)
    monkeypatch.setattr(container, "start_managed_command", start)
    return session, host, container, captured


@pytest.mark.parametrize("container", [False, True])
async def test_remote_budget_is_persisted_and_executes_inside_the_managed_runtime(
    executors, container
):
    session, host, ctr, captured = executors
    executor = ctr if container else host
    executor.bind_run_workspace(str(uuid.uuid4()))
    handle = await executor.launch_managed_run(timeout_seconds=123.25)
    assert handle.context.hard_timeout_seconds == 123.25
    material = "\n".join(session.files.values()) if container else captured[0][1]
    assert "timeout --signal=TERM --kill-after=5s 123.250000s stdbuf" in material
    if container:
        assert "docker exec" in captured[0][1] and "setsid bash" in captured[0][1]
        assert handle.attempt_id in captured[0][1]
        assert any("command -v timeout" in command and "docker exec" in command
                   for command in session.commands)


@pytest.mark.parametrize("container", [False, True])
@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf"), True, "1; evil", 1e-12])
async def test_invalid_remote_deadline_cannot_reach_shell(executors, container, invalid):
    session, host, ctr, captured = executors
    with pytest.raises(ValueError):
        await (ctr if container else host).launch_managed_run(timeout_seconds=invalid)
    assert session.commands == [] and captured == []


@pytest.mark.parametrize("container", [False, True])
async def test_missing_remote_timeout_refuses_budgeted_launch(executors, container):
    session, host, ctr, captured = executors
    session.timeout_available = False
    from app.services.managed_commands import FailureDomain, RepairScope, failure_from_exception

    with pytest.raises(SSHExecError, match="requires") as failed:
        await (ctr if container else host).launch_managed_run(timeout_seconds=10)
    report = failure_from_exception(failed.value)
    assert report.domain == FailureDomain.ENVIRONMENT
    assert report.repair_scope == RepairScope.INFRASTRUCTURE
    assert captured == []


async def test_container_requires_process_group_support_before_launch(executors):
    session, host, ctr, captured = executors
    session.setsid_available = False
    with pytest.raises(SSHExecError, match="setsid"):
        await ctr.launch_managed_run()
    assert captured == []


@pytest.mark.parametrize("operation", ["run", "smoke", "setup", "plot", "plot_deps"])
async def test_contained_identity_precedes_user_code_and_wrapper_has_valid_shell(
    executors, operation
):
    import subprocess

    session, host, ctr, captured = executors
    handle = await getattr(ctr, f"launch_managed_{operation}")()
    assert len(session.files) == 1
    path, launcher = next(iter(session.files.items()))
    assert handle.attempt_id in path and handle.attempt_id in captured[0][1]
    assert ".polaris/container/" in path
    assert launcher.index("${prefix}.pgid") < launcher.index("{\n")
    parsed = subprocess.run(["bash", "-n"], input=launcher, text=True, capture_output=True)
    assert parsed.returncode == 0, parsed.stderr


@pytest.mark.parametrize("state", ["running", "unknown", "exited"])
async def test_host_exit_requires_contained_group_proof(executors, monkeypatch, state):
    session, host, ctr, captured = executors
    handle = await ctr.launch_managed_run()
    if state == "unknown":
        session.ledger_pgid = None
    if state == "exited":
        session.members = "99 S\n77 Z\n"  # reusable container init and exited job zombie

    async def host_snapshot(self, handle, **kwargs):
        return CommandSnapshot(handle.operation_id, handle.attempt_id, handle.context,
                               1, False, 0)

    monkeypatch.setattr(SSHExecutor, "inspect_managed_command", host_snapshot)
    snapshot = await ctr.inspect_managed_command(handle)
    assert snapshot.process_alive is (state != "exited")
    if state != "exited":
        assert snapshot.process_state == f"contained_process_{state}"
    assert not any(command.startswith("docker stop") for command in session.commands)


@pytest.mark.parametrize("confirmed", [False, True])
async def test_container_stop_verifies_container_after_host_group_stops(executors, monkeypatch,
                                                                     confirmed):
    session, host, ctr, captured = executors
    handle = await ctr.launch_managed_run()
    session.stop_succeeds = confirmed

    async def host_stop(self, handle):
        return ManagedStopResult("stopped", True)

    monkeypatch.setattr(SSHExecutor, "stop_managed_command", host_stop)
    outcome = await ctr.stop_managed_command(handle)
    assert bool(outcome) is confirmed
    assert any(command.startswith("docker stop") for command in session.commands)
    assert outcome.status == ("container_stopped" if confirmed else "container_stop_unconfirmed")


async def test_container_stop_refuses_host_identity_change_before_stopping_container(executors,
                                                                                   monkeypatch):
    session, host, ctr, captured = executors
    handle = await ctr.launch_managed_run()

    async def host_stop(self, handle):
        return ManagedStopResult("attempt_changed", False)

    monkeypatch.setattr(SSHExecutor, "stop_managed_command", host_stop)
    outcome = await ctr.stop_managed_command(handle)
    assert not outcome
    assert not any(command.startswith("docker stop") for command in session.commands)


async def test_cleanup_restores_persisted_container_backend(executors, monkeypatch):
    session, host, ctr, captured = executors

    async def open_executor(**kwargs):
        return host

    monkeypatch.setattr(experiments.ssh_exec, "open_executor", open_executor)
    experiment = SimpleNamespace(id=uuid.UUID(host.exp_id), project_id=host.project_id,
                                 plan={"container": {"image": "fixture:1"}})
    executor = await experiments._open_cleanup_executor(experiment, object())
    assert isinstance(executor, ContainerRunner)
    assert executor._session is session
