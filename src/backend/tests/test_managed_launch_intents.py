"""Managed phase ownership survives interrupted launches and phase resumes."""

import asyncio
import uuid

import pytest

from app.agents.voyage import actions_experiment as ax
from app.core.db import get_sessionmaker
from app.models.experiment import Experiment
from app.models.voyage import VoyageRun
from app.services import experiments
from app.services.managed_commands import OperationContext
from app.services.managed_ssh import ManagedCommandHandle
from tests.test_autoresearch_controller import _ControllerProvider
from tests.test_experiment_iterate import _launch_experiment, _router_with
from tests.test_experiment_iterate import (
    fake_ssh as _shared_fake_ssh,
)
from tests.test_experiment_remote_cleanup import (
    scene as _shared_scene,
)
from tests.test_experiments import _approve_gate, _make_engine

scene = _shared_scene
fake_ssh = _shared_fake_ssh


async def test_old_run_cleanup_cannot_resolve_unmatched_new_phase_launch_intent(scene):
    session, experiment, voyage, run, lease, runner = scene
    new_attempt = str(uuid.uuid4())
    old_dependency = ManagedCommandHandle(
        "dependency-install", str(uuid.uuid4()),
        OperationContext("dependency.install", "dependency-install", "old install"), 5252, 5252,
    )
    old_formal = runner.handle

    async def recover(context):
        return old_dependency if context.operation == "dependency-install" else old_formal

    runner.recover_managed_command = recover
    experiment.status = "failed"
    experiment.iteration_state = {"remote_cleanup": {
        "status": "pending", "launch_attempt_id": new_attempt,
        "operation_context": {"phase": "dependency.install", "operation": "dependency-install",
                              "display_command": "new install"},
    }}
    await session.commit()

    assert await experiments.reconcile_remote_cleanup(session, experiment) is False
    await session.refresh(lease)
    assert lease.released_at is None
    cleanup = experiment.iteration_state["remote_cleanup"]
    assert cleanup["status"] == "pending" and cleanup["launch_attempt_id"] == new_attempt
    # The old run can be accounted for without proving the new phase exited.
    assert run.status == "failed"
    assert old_dependency.attempt_id != new_attempt
    assert all(handle.attempt_id != new_attempt for _, handle in runner.stops)


async def test_dependency_resume_monitors_original_attempt_without_new_container_prepare(
    client, queue_stub, fake_ssh, bus_recorder, monkeypatch,
):
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    engine, _ = _make_engine(_router_with(_ControllerProvider(plan={
        "container": {"image": "fixture:latest"},
    })))
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    old_handle = {
        "operation_id": "dependency-install", "attempt_id": str(uuid.uuid4()),
        "process_id": 500, "process_group_id": 500,
        "context": {"phase": "dependency.install", "operation": "dependency-install",
                    "display_command": "install dependencies"},
    }
    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, uuid.UUID(voyage_id))
        voyage.checkpoint = dict(voyage.checkpoint) | {"managed_command_waiting": old_handle}
        experiment = await session.get(Experiment, uuid.UUID(exp_id))
        experiment.iteration_state = dict(experiment.iteration_state) | {
            "remote_cleanup": {"status": "pending", "handle": old_handle},
        }
        await session.commit()
    monitored = []

    async def interrupt_monitor(ctx, session, executor, experiment, handle, run=None):
        monitored.append(handle)
        raise asyncio.CancelledError("synthetic interruption after choosing resumed handle")

    monkeypatch.setattr(ax, "_monitor_managed_command", interrupt_monitor)
    with pytest.raises(asyncio.CancelledError):
        await engine.resume(uuid.UUID(voyage_id))
    assert len(monitored) == 1
    assert monitored[0].operation_id == old_handle["operation_id"]
    assert monitored[0].attempt_id == old_handle["attempt_id"]
    assert not any("nohup setsid bash " in command and "/operations/environment-prepare/" in command
                   for command in fake_ssh.commands)
    async with get_sessionmaker()() as session:
        experiment = await session.get(Experiment, uuid.UUID(exp_id))
        assert experiment.iteration_state["remote_cleanup"]["handle"] == old_handle


async def test_smoke_resume_does_not_treat_dependency_completion_as_smoke_success(
    client, queue_stub, fake_ssh, bus_recorder, monkeypatch,
):
    from types import SimpleNamespace

    from app.agents.voyage.actions import ActionContext

    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    engine, _ = _make_engine(_router_with(_ControllerProvider()))
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    old_handle = {
        "operation_id": "dependency-install", "attempt_id": str(uuid.uuid4()),
        "process_id": 500, "process_group_id": 500,
        "context": {"phase": "dependency.install", "operation": "dependency-install",
                    "display_command": "install dependencies"},
    }
    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, uuid.UUID(voyage_id))
        ctx = ActionContext(run=voyage, llm=engine._llm,
                            checkpoint=dict(voyage.checkpoint) | {
                                "managed_command_waiting": old_handle,
                            })
    monitored = []

    async def control_monitor(ctx, session, executor, experiment, handle, run=None):
        monitored.append(handle.operation_id)
        if handle.operation_id == "dependency-install":
            return SimpleNamespace(exit_status=0), executor
        raise asyncio.CancelledError("stop after actual smoke operation was launched")

    monkeypatch.setattr(ax, "_monitor_managed_command", control_monitor)
    with pytest.raises(asyncio.CancelledError):
        await ax.experiment_smoke(ctx, {})
    assert monitored == ["dependency-install", "application-smoke"]
