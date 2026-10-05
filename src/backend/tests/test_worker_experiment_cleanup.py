"""Terminal remote cleanup remains scheduled when no Voyage is in flight."""

import pytest

from app.models.experiment import Experiment
from app.services import experiments
from tests.test_experiment_remote_cleanup import scene as cleanup_scene
from worker import tasks

scene = cleanup_scene


class RecordingQueue:
    def __init__(self):
        self.jobs = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args, kwargs))


@pytest.mark.parametrize("entry", [tasks.reconcile_stuck_voyages, tasks.reconcile_stale_voyages])
async def test_startup_and_periodic_retry_terminal_cleanup_without_inflight_voyages(
    scene, monkeypatch, entry
):
    session, experiment, voyage, run, lease, runner = scene
    experiment.status = "cancelled"
    voyage.status = "cancelled"
    await session.commit()
    attempts = []

    async def unavailable_cleanup(session, row):
        attempts.append(row.id)
        return False

    monkeypatch.setattr(experiments, "reconcile_remote_cleanup", unavailable_cleanup)
    queue = RecordingQueue()
    await entry({"redis": queue})
    await entry({"redis": queue})
    assert attempts == [experiment.id, experiment.id]
    assert queue.jobs == []
    assert lease.released_at is None and run.status == "running"


async def test_cleanup_helper_counts_confirmed_experiments_and_ignores_active_controller(
    scene, monkeypatch
):
    session, experiment, voyage, run, lease, runner = scene
    experiment.status = "failed"
    voyage.status = "failed"
    confirmed = Experiment(
        project_id=experiment.project_id, idea_id=experiment.idea_id,
        status="cancelled", iteration_state={"remote_cleanup": {"status": "pending"}},
    )
    active = Experiment(
        project_id=experiment.project_id, idea_id=experiment.idea_id,
        status="running", iteration_state={"remote_cleanup": {"status": "pending"}},
    )
    session.add_all([confirmed, active])
    await session.commit()
    visited = []

    async def cleanup(session, row):
        visited.append(row.id)
        return row.id == confirmed.id

    monkeypatch.setattr(experiments, "reconcile_remote_cleanup", cleanup)
    assert await tasks.reconcile_experiment_cleanup({}) == 1
    assert set(visited) == {experiment.id, confirmed.id}
    assert active.id not in visited


@pytest.mark.parametrize("legacy", ["running_row", "checkpoint_handle"])
async def test_legacy_terminal_work_is_reconciled_without_new_cleanup_marker(
    scene, monkeypatch, legacy
):
    session, experiment, voyage, run, lease, runner = scene
    experiment.status = "cancelled"
    voyage.status = "cancelled"
    handle = experiment.iteration_state["remote_cleanup"]["handle"]
    experiment.iteration_state = {}
    if legacy == "checkpoint_handle":
        run.status = "failed"
        voyage.checkpoint = {"managed_command_waiting": handle}
    await session.commit()
    visited = []

    async def cleanup(session, row):
        visited.append(row.id)
        return False

    monkeypatch.setattr(experiments, "reconcile_remote_cleanup", cleanup)
    assert await tasks.reconcile_experiment_cleanup({}) == 0
    assert visited == [experiment.id]
