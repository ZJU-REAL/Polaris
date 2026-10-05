"""Plotting keeps operation/attempt identity and fresh workspace output."""

import uuid

import pytest

from app.agents.voyage.runner import ContainerRunner, ContainerSpec
from app.services.managed_commands import RepairScope
from app.services.ssh_exec import SSHExecutor
from tests.fake_ssh import FakeSSHServer, FakeSSHSession


@pytest.fixture(params=[False, True], ids=["host", "container"])
def scene(request, monkeypatch):
    async def audit(self, command):
        pass

    monkeypatch.setattr(SSHExecutor, "_audit", audit)
    server = FakeSSHServer()
    fields = {"exp_id": str(uuid.uuid4()), "project_id": uuid.uuid4(), "host": "fixture"}
    executor = (
        ContainerRunner(FakeSSHSession(server), **fields, spec=ContainerSpec(image="fixture:1"))
        if request.param else SSHExecutor(FakeSSHSession(server), **fields)
    )
    workspace = str(uuid.uuid4())
    executor.bind_run_workspace(workspace)
    return server, executor, workspace


async def test_managed_plot_recovers_reserved_identity_and_writes_only_attempt_png(scene):
    server, executor, workspace = scene
    server.plot_outputs = {"figures/result.png": b"current-png"}
    old_root = f"polaris_runs/{executor.exp_id}/figures/result.png"
    server.files[old_root] = b"old-root-png"
    attempt = str(uuid.uuid4())
    handle = await executor.launch_managed_plot(attempt_id=attempt)
    assert handle.operation_id == "experiment-plot"
    assert handle.attempt_id == attempt
    assert handle.context.phase == "application.plot"
    assert handle.context.repair_scope == RepairScope.APPLICATION_FILES
    assert handle.context.hard_timeout_seconds == 300
    recovered = await executor.recover_managed_command(handle.context)
    assert recovered.attempt_id == attempt and recovered.process_id == handle.process_id
    assert await executor.read_file("figures/result.png") == b"current-png"
    assert server.files[old_root] == b"old-root-png"
    path = f"polaris_runs/{executor.exp_id}/.polaris/runs/{workspace}/figures/result.png"
    assert server.files[path] == b"current-png"
    assert any("plot_figures.py" in str(source) for source in server.files.values())


async def test_failed_managed_plot_keeps_diagnostics_without_emitting_png(scene):
    server, executor, _ = scene
    server.plot_exits = [2]
    server.plot_outputs = {"figures/result.png": b"must-not-be-produced"}
    handle = await executor.launch_managed_plot(attempt_id=str(uuid.uuid4()))
    snapshot = await executor._managed_commands().snapshot(handle)
    assert snapshot.exit_status == 2
    assert snapshot.stderr_tail == server.plot_stderr
    with pytest.raises(FileNotFoundError):
        await executor.read_file("figures/result.png")


async def test_managed_plot_dependency_failure_preserves_reserved_identity_and_error(scene):
    server, executor, _ = scene
    server.venv_exit = 1
    attempt = str(uuid.uuid4())
    handle = await executor.launch_managed_plot_deps(attempt_id=attempt)
    assert handle.operation_id == "plot-dependencies" and handle.attempt_id == attempt
    assert handle.context.phase == "dependency.plot"
    assert handle.context.repair_scope == RepairScope.DEPENDENCY_FILES
    recovered = await executor.recover_managed_command(handle.context)
    assert recovered.attempt_id == attempt
    snapshot = await executor._managed_commands().snapshot(handle)
    assert snapshot.exit_status == 1 and snapshot.stderr_tail == "pip failed"
    assert any("import matplotlib" in str(source) for source in server.files.values())
