"""Run workspaces keep candidate output and durable handles separate."""

import uuid

import pytest

from app.services.ssh_exec import SSHExecError, SSHExecutor, SSHPathViolationError, SSHResult


class RecordingSession:
    def __init__(self) -> None:
        self.commands: list[str] = []
        self.files: dict[str, str] = {}
        self.directories: set[str] = set()
        self.fail_on: str | None = None

    async def run(self, command: str, timeout: float | None = None) -> SSHResult:
        self.commands.append(command)
        if self.fail_on and self.fail_on in command:
            return SSHResult(1, "", "failure")
        if " && mkdir " in command:
            directory = command.rsplit(" && mkdir ", 1)[1]
            if directory in self.directories:
                return SSHResult(1, "", "File exists")
            self.directories.add(directory)
        return SSHResult(0, "", "")

    async def write_file(self, path: str, content: str) -> None:
        self.files[path] = content

    async def read_file(self, path: str) -> bytes:
        return self.files[path].encode()

    async def close(self) -> None:
        pass


class RecordingExecutor(SSHExecutor):
    async def _audit(self, command: str) -> None:
        pass


def executor() -> tuple[RecordingExecutor, RecordingSession]:
    session = RecordingSession()
    return RecordingExecutor(
        session, exp_id=str(uuid.uuid4()), host="host", project_id=uuid.uuid4()
    ), session


@pytest.mark.asyncio
async def test_prepare_run_copies_only_candidate_files_and_shares_environment_cache():
    runner, remote = executor()
    root = runner.experiment_workdir
    run_id = str(uuid.uuid4())
    target = await runner.prepare_run_workspace(
        run_id, {"run.sh": "python train.py", "src/train.py": "print(1)"}
    )
    assert target == f"{root}/.polaris/runs/{run_id}"
    assert runner.workdir == target
    assert runner.run_workspace_id == run_id
    sftp_target = f"polaris_runs/{runner.exp_id}/.polaris/runs/{run_id}"
    assert remote.files == {
        f"{sftp_target}/run.sh": "python train.py",
        f"{sftp_target}/src/train.py": "print(1)",
    }
    assert any(f"ln -s ../../../.venv {target}/.venv" in cmd for cmd in remote.commands)
    assert any(f"ln -s ../../../data_cache {target}/data_cache" in cmd for cmd in remote.commands)
    support_copy = next(cmd for cmd in remote.commands if cmd.startswith("for support"))
    assert "for support in env.sh llm_config.json;" in support_copy
    assert f"cp {root}/$support {target}/$support" in support_copy
    assert not any("metrics.json" in cmd for cmd in remote.commands)
    assert runner._managed_commands()._shell_workdir == target
    await runner.read_metrics_json()
    assert remote.commands[-1] == f"cat {target}/metrics.json 2>/dev/null"


@pytest.mark.asyncio
async def test_explicit_platform_env_is_not_overwritten_from_root():
    runner, remote = executor()
    await runner.prepare_run_workspace(str(uuid.uuid4()), {"env.sh": "export X=1"})
    support_copy = next(cmd for cmd in remote.commands if cmd.startswith("for support"))
    assert "for support in llm_config.json;" in support_copy


@pytest.mark.asyncio
async def test_existing_run_directory_is_never_overwritten():
    runner, remote = executor()
    run_id = str(uuid.uuid4())
    await runner.prepare_run_workspace(run_id, {"run.sh": "first"})
    first_files = dict(remote.files)
    with pytest.raises(SSHExecError, match="拒绝覆写"):
        await runner.prepare_run_workspace(run_id, {"run.sh": "second"})
    assert remote.files == first_files


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path", ["../escape.py", ".polaris/fake", ".venv/bin/python", "data_cache/source.py",
             "metrics.json", "run.log", "run.exit", "setup.exit"]
)
async def test_reserved_and_escaping_files_fail_before_remote_mutation(path):
    runner, remote = executor()
    with pytest.raises(SSHPathViolationError):
        await runner.prepare_run_workspace(str(uuid.uuid4()), {"run.sh": "safe", path: "bad"})
    assert remote.commands == []
    assert remote.files == {}
    assert runner.run_workspace_id is None


@pytest.mark.asyncio
async def test_preparation_failure_restores_prior_binding():
    runner, remote = executor()
    previous = str(uuid.uuid4())
    runner.bind_run_workspace(previous)
    remote.fail_on = "ln -s"
    with pytest.raises(SSHExecError, match="链接失败"):
        await runner.prepare_run_workspace(str(uuid.uuid4()), {"run.sh": "safe"})
    assert runner.run_workspace_id == previous
    assert remote.files == {}


def test_recovery_binding_validates_uuid_and_does_not_touch_remote_files():
    runner, remote = executor()
    root = runner.workdir
    run_id = str(uuid.uuid4())
    runner.bind_run_workspace(run_id)
    assert runner._managed_commands()._sftp_workdir.endswith(f"/.polaris/runs/{run_id}")
    assert remote.commands == []
    with pytest.raises(ValueError):
        runner.bind_run_workspace("bad; command")
    assert runner.run_workspace_id == run_id
    runner.bind_run_workspace(None)
    assert runner.workdir == root


@pytest.mark.asyncio
async def test_nested_candidate_parent_is_shell_quoted():
    runner, remote = executor()
    await runner.prepare_run_workspace(str(uuid.uuid4()), {"source; echo unsafe/run.sh": "safe"})
    assert f"mkdir -p {runner.workdir}/'source; echo unsafe'" in remote.commands



@pytest.mark.parametrize("container", [False, True])
async def test_plot_fixture_writes_only_into_selected_run_workspace(container):
    from tests.fake_ssh import FakeSSHServer, FakeSSHSession

    exp_id, run_id = str(uuid.uuid4()), str(uuid.uuid4())
    server = FakeSSHServer(plot_outputs={"figures/result.png": b"fixture plot"})
    remote = FakeSSHSession(server)
    if container:
        command = (f"docker exec polaris_{exp_id} bash -lc "
                   f"'cd /work/.polaris/runs/{run_id} && python plot_figures.py'")
    else:
        command = (f"cd ~/polaris_runs/{exp_id}/.polaris/runs/{run_id} "
                   "&& .venv/bin/python plot_figures.py")
    result = await remote.run(command)
    assert result.exit_status == 0
    assert server.files == {
        f"polaris_runs/{exp_id}/.polaris/runs/{run_id}/figures/result.png": b"fixture plot",
    }
