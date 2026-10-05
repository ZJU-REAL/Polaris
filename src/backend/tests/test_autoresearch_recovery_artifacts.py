"""Offline interruption and artifact-provenance regressions for the real controller."""

import asyncio
import json
import re
import uuid

import pytest
from sqlalchemy import func, select

from app.agents.voyage import actions_experiment as ax
from app.agents.voyage.actions import ActionContext
from app.core.db import get_sessionmaker
from app.models.activity import Activity
from app.models.experiment import Experiment, ExperimentRun
from app.models.voyage import VoyageRun
from app.services.ssh_exec import SSHExecutor
from tests.fake_ssh import FakeSSHSession
from tests.test_autoresearch_controller import _ControllerProvider
from tests.test_experiment_iterate import (
    _drive_pipeline,
    _get_detail,
    _launch_experiment,
    _result,
    _router_with,
)
from tests.test_experiment_iterate import fake_ssh as _shared_fake_ssh
from tests.test_experiments import FAKE_PNG, _approve_gate, _make_engine, metric_log
from tests.test_manuscripts import _create_manuscript

fake_ssh = _shared_fake_ssh


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(ax, "RUN_POLL_SECONDS", 0)


@pytest.mark.parametrize("interruption", ["before_prepare", "during_source_write"])
async def test_reserved_run_recovers_with_complete_source_before_any_launch(
    client,
    queue_stub,
    fake_ssh,
    bus_recorder,
    monkeypatch,
    interruption,
):
    fake_ssh.run_log = metric_log(0.91)
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    engine, _ = _make_engine(_router_with(_ControllerProvider(decisions=["stop"])))
    interrupted = False
    original_prepare = SSHExecutor.prepare_run_workspace
    original_write = FakeSSHSession.write_file

    async def controlled_prepare(executor, workspace_id, files):
        nonlocal interrupted
        if interruption == "before_prepare" and not interrupted:
            interrupted = True
            raise asyncio.CancelledError("synthetic crash before remote preparation")
        return await original_prepare(executor, workspace_id, files)

    async def controlled_write(session, path, content):
        nonlocal interrupted
        await original_write(session, path, content)
        if (
            interruption == "during_source_write"
            and not interrupted
            and "/.polaris/runs/" in path
            and path.endswith("/requirements.txt")
        ):
            interrupted = True
            raise asyncio.CancelledError("synthetic crash after first source file")

    monkeypatch.setattr(SSHExecutor, "prepare_run_workspace", controlled_prepare)
    monkeypatch.setattr(FakeSSHSession, "write_file", controlled_write)
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    with pytest.raises(asyncio.CancelledError):
        await engine.resume(uuid.UUID(voyage_id))
    assert interrupted
    async with get_sessionmaker()() as session:
        experiment = await session.get(Experiment, uuid.UUID(exp_id))
        runs = list(
            await session.scalars(
                select(ExperimentRun).where(ExperimentRun.experiment_id == experiment.id)
            )
        )
        assert len(runs) == 1 and runs[0].pid is None and runs[0].status == "running"
        metadata = experiment.iteration_state["run_metadata"][str(runs[0].id)]
        abandoned = metadata["workspace_id"]
        assert not metadata.get("workspace_prepared")
        candidate = experiment.iteration_state["candidates"][0]
        assert not any(
            "nohup setsid bash " in command and "/operations/experiment-run/" in command
            for command in fake_ssh.commands
        )
    launches = []

    async def inspect_source_at_launch(command):
        if "nohup setsid bash " not in command or "/operations/experiment-run/" not in command:
            return
        workspace = re.search(r"polaris_runs/[^/]+/\.polaris/runs/([0-9a-f-]{36})", command)
        assert workspace is not None
        workspace_id = workspace.group(1)
        prefix = workspace.group()
        launches.append(workspace_id)
        assert workspace_id != abandoned
        assert {
            name: fake_ssh.files.get(prefix + "/" + name) for name in candidate["files"]
        } == candidate["files"]
        async with get_sessionmaker()() as session:
            experiment = await session.get(Experiment, uuid.UUID(exp_id))
            metadata = next(iter(experiment.iteration_state["run_metadata"].values()))
            assert metadata["workspace_prepared"] is True
            assert metadata["workspace_id"] == workspace_id
            assert abandoned in metadata["abandoned_workspace_ids"]

    fake_ssh.on_command = inspect_source_at_launch
    await engine.resume(uuid.UUID(voyage_id))
    detail = await _get_detail(client, headers, exp_id)
    voyage_state = (await client.get(f"/api/voyages/{voyage_id}", headers=headers)).json()
    assert detail["status"] == "done", json.dumps(
        [
            step.get("observation")
            for step in voyage_state["steps"]
            if step["action"] == "experiment.run"
        ],
        ensure_ascii=False,
        indent=2,
    )
    assert len(launches) == 1
    bundle = detail["iteration_state"]["result_bundle"]
    assert [evaluation["primary_value"] for evaluation in bundle["valid_evaluations"]] == [0.91]
    assert bundle["selected_candidate"]["files"] == candidate["files"]


async def test_successful_plot_with_no_new_output_cannot_relabel_stale_root_figure(
    client,
    queue_stub,
    fake_ssh,
    bus_recorder,
):
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    stale_path = f"polaris_runs/{exp_id}/figures/old_result.png"
    fake_ssh.files[stale_path] = FAKE_PNG
    fake_ssh.plot_outputs = {}
    await _drive_pipeline(
        client,
        headers,
        project_id,
        voyage_id,
        _router_with(_ControllerProvider(decisions=["stop"])),
    )
    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done", detail
    assert detail["figures"] == []
    assert fake_ssh.files[stale_path] == FAKE_PNG
    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, uuid.UUID(voyage_id))
        attempts = voyage.checkpoint["plot_attempts"]
        assert len(attempts) >= 1
        assert len({attempt["workspace_id"] for attempt in attempts}) == len(attempts)
        assert all(
            attempt["bundle_id"] == detail["iteration_state"]["result_bundle"]["bundle_id"]
            for attempt in attempts
        )
    figure_lists = [
        command
        for command in fake_ssh.commands
        if command.startswith("ls -1") and "/figures" in command
    ]
    assert figure_lists and all("/.polaris/runs/" in command for command in figure_lists)


async def test_rejected_foreign_experiment_does_not_write_its_activity_stream(
    client, queue_stub, bus_recorder
):
    _, headers, exp_id, _ = await _launch_experiment(client)
    other_project_id = (
        await client.post("/api/projects", json={"name": "other scope"}, headers=headers)
    ).json()["id"]
    async with get_sessionmaker()() as session:
        run = VoyageRun(
            kind="demo",
            goal="generic step",
            status="executing",
            project_id=uuid.UUID(other_project_id),
        )
        session.add(run)
        await session.commit()
        count_before = await session.scalar(select(func.count()).select_from(Activity))
    context = ActionContext(run=run, llm=None, checkpoint={"params": {"experiment_id": exp_id}})
    with pytest.raises(ValueError, match="unavailable in this Voyage"):
        await ax.experiment_plan(context, {})
    async with get_sessionmaker()() as session:
        assert await session.scalar(select(func.count()).select_from(Activity)) == count_before
        experiment = await session.get(Experiment, uuid.UUID(exp_id))
        assert experiment.plan is None


async def test_minimize_exact_selector_stays_consistent_through_incumbent_and_fact_pack(
    client,
    queue_stub,
    fake_ssh,
    bus_recorder,
):
    selector = "loss/model/method"
    fake_ssh.run_logs = [
        "POLARIS_METRIC " + json.dumps({"name": selector, "step": 1, "value": value}) + "\n"
        for value in (0.9, 0.7, 0.6)
    ]
    provider = _ControllerProvider(
        plan={"primary_metric": {"name": "loss", "selector": selector, "direction": "minimize"}}
    )
    project_id, headers, exp_id, voyage_id = await _launch_experiment(
        client, budget={"max_hours": 2, "max_runs": 3}
    )
    await _drive_pipeline(client, headers, project_id, voyage_id, _router_with(provider))
    detail = await _get_detail(client, headers, exp_id)
    assert detail["iteration_state"]["incumbent"]["primary_value"] == 0.6
    assert [promotion["accepted"] for promotion in detail["iteration_state"]["promotions"]] == [
        True,
        True,
        True,
    ]
    response = await _create_manuscript(
        client, headers, project_id, idea_id=detail["idea_id"], experiment_id=exp_id
    )
    assert response.status_code == 201, response.text
    manuscript = (
        await client.get(f"/api/manuscripts/{response.json()['id']}", headers=headers)
    ).json()
    metric = next(item for item in manuscript["fact_pack"]["metrics"] if item["name"] == selector)
    assert metric["best"] == 0.6
    assert [entry["value"] for entry in metric["runs"] if entry["selected"]] == [0.6]
    assert (
        manuscript["fact_pack"]["evidence_bundle"]["selected_candidate_id"]
        == (detail["iteration_state"]["incumbent"]["candidate_id"])
    )


async def test_cancellation_during_final_source_check_cannot_publish_scientific_results(
    client,
    queue_stub,
    fake_ssh,
    bus_recorder,
    monkeypatch,
):
    fake_ssh.run_log = metric_log(0.99)
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    engine, _ = _make_engine(_router_with(_ControllerProvider(decisions=["stop"])))
    checked, release = asyncio.Event(), asyncio.Event()
    original_read = SSHExecutor.read_file

    async def controlled_read(executor, name):
        result = await original_read(executor, name)
        if executor.run_workspace_id and name == "train.py":
            checked.set()
            await release.wait()
        return result

    monkeypatch.setattr(SSHExecutor, "read_file", controlled_read)
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    task = asyncio.create_task(engine.resume(uuid.UUID(voyage_id)))
    try:
        await asyncio.wait_for(checked.wait(), 2)
        async with get_sessionmaker()() as session:
            voyage = await session.get(VoyageRun, uuid.UUID(voyage_id))
            experiment = await session.get(Experiment, uuid.UUID(exp_id))
            voyage.status = "cancelled"
            experiment.status = "cancelled"
            await session.commit()
        release.set()
        await asyncio.wait_for(task, 2)
        detail = await _get_detail(client, headers, exp_id)
        state = detail["iteration_state"]
        assert detail["status"] == "cancelled"
        assert not state.get("evaluations")
        assert not state.get("promotions")
        assert not state.get("incumbent")
        assert not state.get("result_bundle")
        assert detail["runs"][0]["status"] == "succeeded"
        assert detail["runs"][0]["primary_value"] == 0.99
        response = await _create_manuscript(
            client, headers, project_id, idea_id=detail["idea_id"], experiment_id=exp_id
        )
        assert response.status_code == 201, response.text
        manuscript = (
            await client.get(f"/api/manuscripts/{response.json()['id']}", headers=headers)
        ).json()
        assert manuscript["fact_pack"]["metrics"] == []
        assert manuscript["fact_pack"]["evidence_bundle"]["provenance"] == "versioned_result_bundle"
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class _ExtraSourceProvider(_ControllerProvider):
    async def complete(self, messages, *, model, temperature=0.7, max_tokens=None, images=None):
        result = await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, images=images
        )
        system = "\n".join(message.text for message in messages if message.role == "system")
        if not images and '"requirements.txt"' in system and self.code_calls > 1:
            payload = json.loads(result.content)
            payload["files"]["helper.py"] = "# Later-candidate-only implementation\n"
            return _result(json.dumps(payload, ensure_ascii=False), model)
        return result


async def test_selected_delivery_removes_only_obsolete_candidate_owned_source(
    client,
    queue_stub,
    fake_ssh,
    bus_recorder,
):
    fake_ssh.run_logs = [metric_log(0.9), metric_log(0.7), metric_log(0.6)]
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    unknown = f"polaris_runs/{exp_id}/user_notes.txt"
    fake_ssh.files[unknown] = "user asset stays"
    await _drive_pipeline(
        client, headers, project_id, voyage_id, _router_with(_ExtraSourceProvider())
    )
    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done", detail
    candidates = detail["iteration_state"]["candidates"]
    assert "helper.py" not in candidates[0]["files"]
    assert "helper.py" in candidates[1]["files"]
    assert "helper.py" in candidates[2]["files"]
    assert f"polaris_runs/{exp_id}/helper.py" not in fake_ssh.files
    assert fake_ssh.files[unknown] == "user asset stays"
    selected = detail["iteration_state"]["result_bundle"]["selected_candidate"]
    assert selected["files"] == candidates[0]["files"]
    assert {
        name: fake_ssh.files[f"polaris_runs/{exp_id}/" + name] for name in selected["files"]
    } == selected["files"]


async def test_truncated_final_metric_record_cannot_reuse_prior_finite_score(
    client,
    queue_stub,
    fake_ssh,
    bus_recorder,
):
    fake_ssh.run_logs = [
        metric_log(0.6),
        metric_log(0.99) + 'POLARIS_METRIC {"name":"accuracy","value":',
    ]
    project_id, headers, exp_id, voyage_id = await _launch_experiment(
        client, budget={"max_hours": 2, "max_runs": 2}
    )
    await _drive_pipeline(
        client, headers, project_id, voyage_id, _router_with(_ControllerProvider())
    )
    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done", detail
    state = detail["iteration_state"]
    assert state["incumbent"]["primary_value"] == 0.6
    assert state["no_improve_streak"] == 0
    assert state["invalid_evaluation_count"] == 1
    assert [entry["accepted"] for entry in state["promotions"]] == [True, False]
    bundle = state["result_bundle"]
    assert [evaluation["seq"] for evaluation in bundle["valid_evaluations"]] == [1]
    invalid = bundle["invalid_evaluations"][0]
    assert invalid["seq"] == 2
    assert any("protocol_error" in reason for reason in invalid["invalid_reasons"])
    assert all(point["value"] == 0.6 for point in detail["metrics"]["accuracy"])


@pytest.mark.parametrize("operation", ["dependency-install", "application-smoke"])
async def test_managed_phase_intent_is_committed_before_remote_launch(
    client, queue_stub, fake_ssh, bus_recorder, operation,
):
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    engine, _ = _make_engine(_router_with(_ControllerProvider(decisions=["stop"])))
    await engine.run(uuid.UUID(voyage_id))
    await _approve_gate(client, headers, project_id)
    observed = []

    async def interrupt_launch(command):
        if "nohup setsid bash " not in command or f"/operations/{operation}/" not in command:
            return
        async with get_sessionmaker()() as session:
            experiment = await session.get(Experiment, uuid.UUID(exp_id))
            intent = experiment.iteration_state["remote_cleanup"]
            assert intent["operation_context"]["operation"] == operation
            assert f"/attempts/{intent['launch_attempt_id']}.sh" in command
            observed.append(intent["launch_attempt_id"])
        raise asyncio.CancelledError("synthetic crash during phase launch")

    fake_ssh.on_command = interrupt_launch
    with pytest.raises(asyncio.CancelledError):
        await engine.resume(uuid.UUID(voyage_id))
    assert len(observed) == 1
    async with get_sessionmaker()() as session:
        experiment = await session.get(Experiment, uuid.UUID(exp_id))
        intent = experiment.iteration_state["remote_cleanup"]
        assert intent["launch_attempt_id"] == observed[0]
        assert intent["status"] == "pending"


async def test_cancel_during_ssh_connect_does_not_reserve_or_launch_new_formal_run(
    client, queue_stub, fake_ssh, bus_recorder, monkeypatch,
):
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    original_open = ax._open_executor
    cancelled = False

    async def cancel_before_reservation(session, ctx, experiment):
        nonlocal cancelled
        executor = await original_open(session, ctx, experiment)
        if experiment.status == "running" and not cancelled:
            cancelled = True
            async with get_sessionmaker()() as cancelling:
                row = await cancelling.get(Experiment, experiment.id)
                await ax.experiments_service.cancel_experiment(cancelling, row)
        return executor

    monkeypatch.setattr(ax, "_open_executor", cancel_before_reservation)
    await _drive_pipeline(
        client, headers, project_id, voyage_id,
        _router_with(_ControllerProvider(decisions=["stop"])),
    )
    assert cancelled
    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "cancelled"
    assert detail["runs"] == []
    assert not detail["iteration_state"].get("incumbent")
    assert not any("nohup setsid bash " in command and "/operations/experiment-run/" in command
                   for command in fake_ssh.commands)


@pytest.mark.parametrize("operation", ["plot-dependencies", "experiment-plot"])
async def test_cancel_during_plot_launch_preserves_intent_and_does_not_publish_figures(
    client, queue_stub, fake_ssh, bus_recorder, operation,
):
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    seen = []

    async def cancel_launch(command):
        if "nohup setsid bash " not in command or f"/operations/{operation}/" not in command:
            return
        async with get_sessionmaker()() as session:
            row = await session.get(Experiment, uuid.UUID(exp_id))
            intent = row.iteration_state["remote_cleanup"]
            assert intent["operation_context"]["operation"] == operation
            assert f"/attempts/{intent['launch_attempt_id']}.sh" in command
            await ax.experiments_service.cancel_experiment(session, row)
            # PID publication is still in flight. Unknown remote state retains ownership.
            assert row.iteration_state["remote_cleanup"]["status"] == "pending"
            seen.append(intent["launch_attempt_id"])

    fake_ssh.on_command = cancel_launch
    await _drive_pipeline(
        client, headers, project_id, voyage_id,
        _router_with(_ControllerProvider(decisions=["stop"])),
    )
    assert len(seen) == 1
    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "cancelled"
    assert detail["figures"] == []
    assert detail["iteration_state"]["result_bundle"]["selected_candidate"] is not None
    if operation == "plot-dependencies":
        assert not any("nohup setsid bash " in command and "/operations/experiment-plot/" in command
                       for command in fake_ssh.commands)
