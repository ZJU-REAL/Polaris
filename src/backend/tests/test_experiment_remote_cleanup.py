"""External lifecycle reconciliation on disposable SQLite and in-memory runners."""

import uuid
from dataclasses import asdict

import pytest
import pytest_asyncio

from app.core.db import get_sessionmaker
from app.models.experiment import Experiment, ExperimentRun
from app.models.idea import Idea
from app.models.project import Project
from app.models.resource import Resource, ResourceLease
from app.models.ssh_credential import SSHCredential
from app.models.user import User
from app.models.voyage import VoyageRun
from app.services import experiments, resource_leases
from app.services.managed_commands import OperationContext
from app.services.managed_ssh import ManagedCommandHandle, ManagedGPUUsage, ManagedStopResult


class MemoryRunner:
    def __init__(self, handle):
        self.handle = handle
        self.workspace = None
        self.stops = []
        self.recoveries = []
        self.kills = []
        self.outcome = ManagedStopResult("stopped", True)
        self.error = None
        self.on_stop = None

    def bind_run_workspace(self, workspace):
        self.workspace = workspace

    async def stop_managed_command(self, handle):
        self.stops.append((self.workspace, handle))
        if self.on_stop:
            await self.on_stop()
        if self.error:
            raise self.error
        return self.outcome

    async def recover_managed_command(self, context):
        self.recoveries.append(self.workspace)
        return self.handle

    async def managed_command_gpu_usage(self, handle):
        self.stops.append((self.workspace, handle))
        return ManagedGPUUsage("idle", True)

    async def kill_pid(self, pid):
        self.kills.append(pid)

    async def check_pid(self, pid):
        return False

    async def close(self):
        pass


@pytest_asyncio.fixture
async def scene(app, monkeypatch):
    async with get_sessionmaker()() as session:
        user = User(email="cleanup@example.com", hashed_password="fixture", is_active=True)
        session.add(user)
        await session.flush()
        project = Project(name="cleanup", slug="cleanup", owner_id=user.id)
        session.add(project)
        await session.flush()
        idea = Idea(project_id=project.id, title="test", status="promoted")
        voyage = VoyageRun(project_id=project.id, created_by=user.id,
                           goal="test cleanup", kind="experiment", status="executing")
        credential = SSHCredential(user_id=user.id, name="fixture", host="fixture",
                                   username="fixture", private_key_encrypted="fixture")
        session.add_all([idea, voyage, credential])
        await session.flush()
        experiment = Experiment(project_id=project.id, idea_id=idea.id,
                                voyage_id=voyage.id, credential_id=credential.id,
                                status="running")
        session.add(experiment)
        await session.flush()
        run = ExperimentRun(experiment_id=experiment.id, seq=1, command="bash run.sh",
                            status="running", pid=4242)
        resource = Resource(owner_id=user.id, name="fixture", kind="host")
        session.add_all([run, resource])
        await session.flush()
        handle = ManagedCommandHandle(
            operation_id="experiment-run", attempt_id=str(uuid.uuid4()),
            context=OperationContext("application.run", "experiment-run", "bash run.sh"),
            process_id=4242, process_group_id=4242,
        )
        experiment.iteration_state = {
            "run_metadata": {str(run.id): {"workspace_id": str(run.id)}},
            "remote_cleanup": {"status": "pending", "workspace_id": str(run.id),
                               "handle": asdict(handle)},
        }
        lease = ResourceLease(resource_id=resource.id, run_id=voyage.id)
        session.add(lease)
        await session.commit()
        runner = MemoryRunner(handle)

        async def open_executor(**kwargs):
            return runner

        monkeypatch.setattr(experiments.ssh_exec, "open_executor", open_executor)
        yield session, experiment, voyage, run, lease, runner


@pytest.mark.parametrize(
    "status", ["stop_unconfirmed", "attempt_changed", "group_probe_unavailable"]
)
async def test_cancel_preserves_running_row_and_resource_until_verified_retry(scene, status):
    session, experiment, voyage, run, lease, runner = scene
    runner.outcome = ManagedStopResult(status, False)
    await experiments.cancel_experiment(session, experiment)
    assert experiment.status == "cancelled" and voyage.status == "cancelled"
    assert run.status == "running" and run.finished_at is None
    assert experiment.iteration_state["remote_cleanup"]["stop_status"] == status
    assert lease.released_at is None
    assert await resource_leases.release_for_run(session, voyage.id) == 0
    assert runner.kills == []
    assert runner.stops[0][0] == str(run.id)

    runner.outcome = ManagedStopResult("stopped", True)
    assert await experiments.reconcile_remote_cleanup(session, experiment) is True
    await session.refresh(lease)
    assert run.status == "failed" and run.exit_code == -15 and run.finished_at is not None
    assert "remote_cleanup" not in experiment.iteration_state
    assert lease.released_at is not None


async def test_ssh_failure_keeps_cancellation_durable_and_resource_reserved(scene):
    session, experiment, voyage, run, lease, runner = scene
    runner.error = ConnectionError("fixture transport unavailable")
    await experiments.cancel_experiment(session, experiment)
    assert experiment.status == "cancelled"
    assert run.status == "running" and lease.released_at is None
    status = experiment.iteration_state["remote_cleanup"]["stop_status"]
    assert status.startswith("cleanup_unavailable")
    runner.error = None
    assert await experiments.reconcile_remote_cleanup(session, experiment) is True


async def test_recovery_binds_run_workspace_before_recovering_current_handle(scene):
    session, experiment, voyage, run, lease, runner = scene
    experiment.status = "failed"
    experiment.iteration_state = {"run_metadata": experiment.iteration_state["run_metadata"]}
    await session.commit()
    assert await experiments.reconcile_remote_cleanup(session, experiment) is True
    assert runner.recoveries == [str(run.id)]
    assert runner.stops[0][0] == str(run.id)
    await session.refresh(lease)
    assert lease.released_at is not None


async def test_missing_pid_recovers_same_workspace_after_launch_persistence_failure(scene):
    session, experiment, voyage, run, lease, runner = scene
    experiment.status = "failed"
    experiment.iteration_state = {"run_metadata": experiment.iteration_state["run_metadata"],
                                "remote_cleanup": {"status": "pending", "reason": "launching"}}
    run.pid = None
    await session.commit()
    assert await experiments.reconcile_remote_cleanup(session, experiment) is True
    assert runner.recoveries == [str(run.id)]
    assert run.pid == runner.handle.process_id and run.status == "failed"


async def test_invalid_saved_handle_never_falls_back_to_pid_kill(scene):
    session, experiment, voyage, run, lease, runner = scene
    experiment.status = "failed"
    state = dict(experiment.iteration_state)
    state["remote_cleanup"] = {"status": "pending", "handle": {"broken": True}}
    experiment.iteration_state = state
    await session.commit()
    assert await experiments.reconcile_remote_cleanup(session, experiment) is False
    assert runner.kills == [] and runner.stops == []
    assert run.status == "running" and lease.released_at is None


async def test_missing_managed_pointer_in_new_workspace_remains_unconfirmed(scene):
    session, experiment, voyage, run, lease, runner = scene
    experiment.status = "failed"
    experiment.iteration_state = {"run_metadata": experiment.iteration_state["run_metadata"]}
    runner.handle = None
    await session.commit()
    assert await experiments.reconcile_remote_cleanup(session, experiment) is False
    assert runner.kills == [] and run.status == "running"
    assert lease.released_at is None


@pytest.mark.parametrize("operation", ["stop", "gpu"])
async def test_user_managed_command_action_restores_the_same_workspace(scene, operation):
    session, experiment, voyage, run, lease, runner = scene
    helper = (experiments.stop_managed_command_by_voyage if operation == "stop"
              else experiments.managed_command_gpu_usage_by_voyage)
    await helper(session, voyage.id, asdict(runner.handle))
    assert runner.stops[0][0] == str(run.id)


async def test_concurrent_successor_cleanup_marker_cannot_be_cleared_by_old_stop(scene):
    session, experiment, voyage, run, lease, runner = scene
    experiment.status = "failed"
    await session.commit()
    successor = {"status": "pending", "workspace_id": str(uuid.uuid4()),
                 "handle": {**asdict(runner.handle), "attempt_id": str(uuid.uuid4())}}

    async def concurrent_marker():
        async with get_sessionmaker()() as other:
            row = await other.get(Experiment, experiment.id)
            row.iteration_state = {**row.iteration_state, "remote_cleanup": successor}
            await other.commit()

    runner.on_stop = concurrent_marker
    assert await experiments.reconcile_remote_cleanup(session, experiment) is False
    assert experiment.iteration_state["remote_cleanup"] == successor
    assert lease.released_at is None



async def test_resource_release_refreshes_cached_experiment_before_remote_exit_check(scene):
    session, experiment, voyage, run, lease, runner = scene
    experiment.status = "failed"
    experiment.iteration_state = {}
    run.status = "succeeded"
    await session.commit()
    async with get_sessionmaker()() as other:
        row = await other.get(Experiment, experiment.id)
        row.iteration_state = {"remote_cleanup": {"status": "pending"}}
        await other.commit()
    assert await resource_leases.release_for_run(session, voyage.id) == 0
    assert lease.released_at is None



@pytest.mark.parametrize("confirmed", [True, False])
async def test_monitor_enforces_persistent_absolute_deadline_despite_fresh_output(scene, confirmed):
    from datetime import timedelta

    from app.agents.voyage import actions_experiment as ax
    from app.agents.voyage.actions import ActionContext
    from app.models.base import utcnow
    from app.services.managed_commands import CommandSnapshot

    session, experiment, voyage, run, lease, runner = scene
    experiment.budget = {"max_hours": 1}
    started = (utcnow() - timedelta(hours=2)).isoformat()
    experiment.iteration_state = {**experiment.iteration_state, "execution_started_at": started}
    await session.commit()
    ctx = ActionContext(run=voyage, llm=None, checkpoint={
        "iterate": {"started_at": started},
    })
    runner.run_workspace_id = str(run.id)
    runner.outcome = ManagedStopResult("stopped" if confirmed else "stop_unconfirmed", confirmed)

    async def output(handle, **kwargs):
        return [], 0, 0

    async def inspect(handle, **kwargs):
        return CommandSnapshot(
            operation_id=handle.operation_id, attempt_id=handle.attempt_id,
            context=handle.context, elapsed_seconds=1, process_alive=True, exit_status=None,
            stdout_tail="healthy training heartbeat", output_changed=True, seconds_since_output=0,
        )

    runner.read_managed_output = output
    runner.inspect_managed_command = inspect
    if confirmed:
        snapshot, same_runner = await ax._monitor_managed_command(
            ctx, session, runner, experiment, runner.handle, run=run
        )
        assert snapshot.exit_status == -15 and snapshot.process_alive is False
        assert same_runner is runner
        assert "remote_cleanup" not in experiment.iteration_state
    else:
        with pytest.raises(ax.ManagedCommandNeedsUser):
            await ax._monitor_managed_command(
                ctx, session, runner, experiment, runner.handle, run=run
            )
        assert run.status == "running" and lease.released_at is None
        assert experiment.iteration_state["remote_cleanup"]["status"] == "pending"
        assert await resource_leases.release_for_run(session, voyage.id) == 0
    assert len(runner.stops) == 1



async def test_verified_cleanup_survives_refresh_of_eager_loaded_run_relationship(scene):
    session, experiment, voyage, run, lease, runner = scene
    await session.refresh(experiment, attribute_names=["runs"])
    await experiments.cancel_experiment(session, experiment)
    await session.refresh(run)
    assert run.status == "failed" and run.exit_code == -15



@pytest.mark.parametrize("unknown", ["corrupt marker", False, []])
async def test_malformed_cleanup_marker_cannot_release_resource_capacity(scene, unknown):
    session, experiment, voyage, run, lease, runner = scene
    experiment.status = "failed"
    experiment.iteration_state = {"remote_cleanup": unknown}
    run.status = "failed"
    await session.commit()
    assert await resource_leases.release_for_run(session, voyage.id) == 0
    assert await experiments.reconcile_remote_cleanup(session, experiment) is False
    assert lease.released_at is None


async def test_partial_cleanup_never_resignals_confirmed_attempt_on_later_retry(scene):
    session, experiment, voyage, run, lease, runner = scene
    experiment.status = "cancelled"
    second = ExperimentRun(experiment_id=experiment.id, seq=2, command="bash run.sh",
                           status="running", pid=5555)
    session.add(second)
    await session.flush()
    state = dict(experiment.iteration_state)
    state["run_metadata"] = {
        **state["run_metadata"], str(second.id): {"workspace_id": str(second.id)}
    }
    experiment.iteration_state = state
    await session.commit()
    second_handle = ManagedCommandHandle("experiment-run", str(uuid.uuid4()), runner.handle.context,
                                         5555, 5555)
    signalled = []
    second_stops = False

    async def recover(context):
        assert runner.workspace == str(second.id)
        return second_handle

    async def stop(handle):
        signalled.append(handle.process_id)
        confirmed = handle.process_id == run.pid or second_stops
        return ManagedStopResult("stopped" if confirmed else "stop_unconfirmed", confirmed)

    runner.recover_managed_command = recover
    runner.stop_managed_command = stop
    assert await experiments.reconcile_remote_cleanup(session, experiment) is False
    cleanup = experiment.iteration_state["remote_cleanup"]
    assert cleanup["pids"] == [5555] and "handle" not in cleanup
    assert run.status == "failed" and second.status == "running"
    second_stops = True
    assert await experiments.reconcile_remote_cleanup(session, experiment) is True
    assert signalled == [4242, 5555, 5555]
    assert second.status == "failed"


@pytest.mark.parametrize("operation,phase", [
    ("environment-prepare", "environment.prepare"),
    ("dependency-install", "dependency.install"),
    ("application-smoke", "application.smoke"),
    ("plot-dependencies", "dependency.plot"),
    ("experiment-plot", "application.plot"),
])
async def test_launch_intent_cleanup_requires_exact_attempt_before_releasing_capacity(
    scene, operation, phase,
):
    session, experiment, voyage, run, lease, runner = scene
    await session.delete(run)
    attempt_id = str(uuid.uuid4())
    context = OperationContext(phase, operation, "fixture managed phase")
    runner.handle = ManagedCommandHandle(operation, str(uuid.uuid4()), context, 4242, 4242)
    experiment.status = "failed"
    experiment.iteration_state = {"remote_cleanup": {
        "status": "pending", "launch_attempt_id": attempt_id,
        "operation_context": {"operation": operation, "phase": phase,
                              "display_command": "fixture managed phase"},
    }}
    await session.commit()
    assert await experiments.reconcile_remote_cleanup(session, experiment) is False
    assert runner.stops == []  # The older pointer is not the reserved launch.
    await session.refresh(lease)
    assert lease.released_at is None
    runner.handle = ManagedCommandHandle(operation, attempt_id, context, 4242, 4242)
    assert await experiments.reconcile_remote_cleanup(session, experiment) is True
    assert runner.stops == [(None, runner.handle)]
    assert "remote_cleanup" not in experiment.iteration_state
    await session.refresh(lease)
    assert lease.released_at is not None


async def test_cancelled_voyage_cannot_reserve_or_launch_managed_phase(scene):
    from app.agents.voyage.actions import ActionContext
    from app.agents.voyage.actions_experiment import _reserve_managed_launch

    session, experiment, voyage, run, lease, runner = scene
    voyage.status = "cancelled"
    await session.commit()
    before = experiment.iteration_state
    ctx = ActionContext(run=voyage, llm=None)
    assert await _reserve_managed_launch(
        ctx, session, runner, experiment, phase="application.smoke",
        operation="application-smoke", command="bash run.sh --smoke",
    ) is None
    await session.refresh(experiment)
    assert experiment.iteration_state == before
