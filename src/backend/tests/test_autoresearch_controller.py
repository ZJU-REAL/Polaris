"""Offline controller regressions for source selection and per-attempt evidence.

Drive the public experiment/Voyage pipeline through its real controller. Only
the LLM and SSH transport are replaced: each candidate has distinct source,
each remote attempt owns its log, and persisted bundles are read through API.
"""

import json
import re
import uuid

import pytest

from app.agents.voyage import actions_experiment as ax
from app.core.db import get_sessionmaker
from app.core.llm.fake import FakeProvider
from app.models.voyage import VoyageRun
from tests.test_experiment_iterate import (
    _drive_pipeline,
    _get_detail,
    _launch_experiment,
    _reflection_json,
    _result,
    _router_with,
)
from tests.test_experiment_iterate import (
    fake_ssh as _shared_fake_ssh,
)
from tests.test_experiments import metric_log

fake_ssh = _shared_fake_ssh


@pytest.fixture(autouse=True)
def fast_poll(monkeypatch):
    monkeypatch.setattr(ax, "RUN_POLL_SECONDS", 0)


class _ControllerProvider(FakeProvider):
    """Keep decisions deterministic while giving candidates different source."""

    def __init__(self, *, plan=None, decisions=None):
        self.plan = plan or {}
        self.decisions = decisions or []
        self.code_calls = 0
        self.reflection_calls = 0

    async def complete(
        self, messages, *, model, temperature=0.7, max_tokens=None, images=None,
    ):
        # Inspect the system stage only: archived user evidence may contain
        # markers for other stages and must not change the test's fake route.
        system = "\n".join(message.text for message in messages if message.role == "system")
        if not images and '"hypothesis_updates"' in system:
            index = self.reflection_calls
            self.reflection_calls += 1
            decision = self.decisions[index] if index < len(self.decisions) else "improve"
            return _result(_reflection_json(
                decision, stop_reason="planned test stop" if decision == "stop" else None,
            ), model)
        if not images and '"requirements.txt"' in system:
            self.code_calls += 1
            payload = json.loads(FakeProvider._respond_exp_code())
            label = chr(ord("A") + self.code_calls - 1)
            payload["files"]["train.py"] = (
                f"# controller candidate {label}\n" + payload["files"]["train.py"]
            )
            return _result(json.dumps(payload, ensure_ascii=False), model)
        if not images and '"repro_strategy"' in system:
            payload = json.loads(FakeProvider._respond_exp_plan())
            payload.update(self.plan)
            return _result(json.dumps(payload, ensure_ascii=False), model)
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, images=images,
        )


async def test_controller_restores_best_source_after_two_worse_candidates(
    client, queue_stub, fake_ssh, bus_recorder,
):
    fake_ssh.run_logs = [metric_log(0.9), metric_log(0.7), metric_log(0.6)]
    provider = _ControllerProvider()
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    await _drive_pipeline(client, headers, project_id, voyage_id, _router_with(provider))

    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"
    assert [run["primary_value"] for run in detail["runs"]] == [0.9, 0.7, 0.6]
    state = detail["iteration_state"]
    assert state["stopped_reason"] == "no_improve"
    assert [promotion["accepted"] for promotion in state["promotions"]] == [True, False, False]
    candidates = state["candidates"]
    assert len(candidates) == 3
    assert [candidate["files"]["train.py"].splitlines()[0] for candidate in candidates] == [
        "# controller candidate A", "# controller candidate B", "# controller candidate C",
    ]
    assert state["working_candidate_id"] == candidates[2]["candidate_id"]
    assert state["incumbent"]["candidate_id"] == candidates[0]["candidate_id"]
    bundle = state["result_bundle"]
    assert bundle["selected_candidate"]["files"] == candidates[0]["files"]
    assert [e["primary_value"] for e in bundle["selected_evaluations"]] == [0.9]
    assert bundle["reproduction"][0]["command"]
    assert bundle["log_references"]
    # Check both persisted delivery state and the restored remote root bytes.
    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, uuid.UUID(voyage_id))
        assert voyage.checkpoint["exp_files"] == candidates[0]["files"]
    assert fake_ssh.files[f"polaris_runs/{exp_id}/train.py"] == candidates[0]["files"]["train.py"]


async def test_controller_failed_partial_score_cannot_promote_or_consume_patience(
    client, queue_stub, fake_ssh, bus_recorder,
):
    fake_ssh.run_logs = [metric_log(0.6), metric_log(0.99), metric_log(0.6), metric_log(0.6)]
    fake_ssh.run_exits = [0, 1, 0, 0]
    provider = _ControllerProvider(decisions=["improve", "debug", "improve", "improve"])
    project_id, headers, exp_id, voyage_id = await _launch_experiment(
        client, budget={"max_hours": 2, "max_runs": 10, "no_improve_stop": 2},
    )
    await _drive_pipeline(client, headers, project_id, voyage_id, _router_with(provider))

    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"
    assert len(detail["runs"]) == 4
    assert [run["status"] for run in detail["runs"]] == [
        "succeeded", "failed", "succeeded", "succeeded",
    ]
    state = detail["iteration_state"]
    assert state["no_improve_streak"] == 2
    assert state["invalid_evaluation_count"] == 1
    assert state["debug_count"] == 1
    assert state["incumbent"]["primary_value"] == 0.6
    assert state["promotions"][1]["reason"] == "invalid_evaluation"
    bundle = state["result_bundle"]
    assert [e["seq"] for e in bundle["valid_evaluations"]] == [1, 3, 4]
    invalid = bundle["invalid_evaluations"][0]
    assert invalid["seq"] == 2
    assert invalid["primary_value"] == 0.99
    assert "run_not_successful" in invalid["invalid_reasons"]
    assert all(point["value"] == 0.6 for point in detail["metrics"]["accuracy"])


async def test_controller_drains_split_final_metric_without_newline_once(
    client, queue_stub, fake_ssh, bus_recorder,
):
    # The metric starts before the SSH read cap and ends after it. There is no
    # final newline, so a terminal drain plus one framer flush are both needed.
    prefix = "padding\n" * 32765
    metric = 'POLARIS_METRIC {"name":"accuracy","step":9,"value":0.91}'
    assert len(prefix.encode()) < 262144 < len((prefix + metric).encode())
    fake_ssh.run_log = prefix + metric
    provider = _ControllerProvider(decisions=["stop"])
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    await _drive_pipeline(client, headers, project_id, voyage_id, _router_with(provider))

    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"
    assert len(detail["runs"]) == 1
    assert detail["runs"][0]["primary_value"] == 0.91
    assert detail["runs"][0]["metrics"]["accuracy"] == [{"step": 9, "value": 0.91}]
    evaluations = detail["iteration_state"]["result_bundle"]["valid_evaluations"]
    assert len(evaluations) == 1
    assert evaluations[0]["metrics"]["accuracy"] == [{"step": 9, "value": 0.91}]
    reads = [command for command in fake_ssh.commands
             if "/operations/experiment-run/" in command and "| head -c 262144" in command]
    assert len(reads) >= 2


async def test_controller_missing_current_control_cannot_borrow_historic_baseline(
    client, queue_stub, fake_ssh, bus_recorder,
):
    def line(name, value):
        return "POLARIS_METRIC " + json.dumps({"name": name, "value": value}) + "\n"

    fake_ssh.run_logs = [
        line("accuracy/model/control", 0.4) + line("accuracy/model/method", 0.6)
        + line("examples", 1),
        line("accuracy/model/method", 0.9) + line("examples", 1),
    ]
    provider = _ControllerProvider(plan={
        "primary_metric": {"name": "accuracy", "direction": "maximize",
                           "selector": "accuracy/model/method"},
        "conditions": [{"name": "control", "role": "baseline"},
                       {"name": "method", "role": "treatment"}],
        "eval_protocol": {"n_examples": 1, "sample_count_metric": "examples"},
    })
    project_id, headers, exp_id, voyage_id = await _launch_experiment(
        client, budget={"max_hours": 2, "max_runs": 2},
    )
    await _drive_pipeline(client, headers, project_id, voyage_id, _router_with(provider))

    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"
    assert len(detail["runs"]) == 2
    assert "accuracy/model/control" not in detail["runs"][1]["metrics"]
    state = detail["iteration_state"]
    assert state["incumbent"]["primary_value"] == 0.6
    assert state["no_improve_streak"] == 0
    assert state["invalid_evaluation_count"] == 1
    bundle = state["result_bundle"]
    assert [e["seq"] for e in bundle["valid_evaluations"]] == [1]
    assert "missing_or_unpaired_conditions" in bundle["invalid_evaluations"][0]["invalid_reasons"]
    assert [p["value"] for p in detail["metrics"]["accuracy/model/method"]] == [0.6]
    assert bundle["selected_evaluations"][0]["primary_value"] == 0.6


@pytest.mark.parametrize("metric_source", ["stream", "json"])
async def test_controller_last_invalid_metric_cannot_fall_back_to_earlier_finite_score(
    client, queue_stub, fake_ssh, bus_recorder, metric_source,
):
    fake_ssh.run_logs = [metric_log(0.6), metric_log(0.99), metric_log(0.6), metric_log(0.6)]
    if metric_source == "stream":
        fake_ssh.run_logs[1] += 'POLARIS_METRIC {"name":"accuracy","step":2,"value":NaN}\n'
    else:
        metric_reads = 0

        async def supply_second_run_invalid_json(command):
            nonlocal metric_reads
            if command.startswith("cat ") and "/metrics.json " in command:
                metric_reads += 1
                fake_ssh.metrics_json = json.dumps({"accuracy": [
                    {"step": 1, "value": 0.99}, {"step": 2, "value": float("nan")},
                ]}) if metric_reads == 2 else None

        fake_ssh.on_command = supply_second_run_invalid_json
    provider = _ControllerProvider()
    project_id, headers, exp_id, voyage_id = await _launch_experiment(
        client, budget={"max_hours": 2, "max_runs": 10, "no_improve_stop": 2},
    )
    await _drive_pipeline(client, headers, project_id, voyage_id, _router_with(provider))

    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"
    assert len(detail["runs"]) == 4
    assert detail["runs"][1]["primary_value"] is None
    assert detail["runs"][1]["metrics"]["accuracy"][-1]["value"] is None
    state = detail["iteration_state"]
    assert state["incumbent"]["primary_value"] == 0.6
    assert state["no_improve_streak"] == 2
    assert state["invalid_evaluation_count"] == 1
    bundle = state["result_bundle"]
    assert [e["seq"] for e in bundle["valid_evaluations"]] == [1, 3, 4]
    invalid = bundle["invalid_evaluations"][0]
    assert invalid["seq"] == 2
    assert "missing_or_non_finite_primary_metric" in invalid["invalid_reasons"]
    assert all(point["value"] == 0.6 for point in detail["metrics"]["accuracy"])
    json.dumps(bundle, allow_nan=False)


async def test_controller_stale_root_metrics_json_cannot_score_a_fresh_run(
    client, queue_stub, fake_ssh, bus_recorder,
):
    fake_ssh.run_logs = [metric_log(0.6), "completed with no metrics\n"]
    project_id, headers, exp_id, voyage_id = await _launch_experiment(
        client, budget={"max_hours": 2, "max_runs": 2},
    )
    stale_path = f"polaris_runs/{exp_id}/metrics.json"
    fake_ssh.files[stale_path] = json.dumps({"accuracy": 0.99})

    async def expose_stale_result_only_if_controller_reads_root(command):
        if command.startswith("cat ") and "/metrics.json " in command:
            fake_ssh.metrics_json = (fake_ssh.files[stale_path]
                                     if "/.polaris/runs/" not in command else None)

    fake_ssh.on_command = expose_stale_result_only_if_controller_reads_root
    await _drive_pipeline(client, headers, project_id, voyage_id,
                         _router_with(_ControllerProvider()))
    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"
    assert detail["runs"][0]["primary_value"] == 0.6
    assert detail["runs"][1]["primary_value"] is None
    state = detail["iteration_state"]
    assert state["incumbent"]["primary_value"] == 0.6
    bundle = state["result_bundle"]
    assert [e["seq"] for e in bundle["valid_evaluations"]] == [1]
    assert "missing_exact_primary_metric" in bundle["invalid_evaluations"][0]["invalid_reasons"]
    reads = [command for command in fake_ssh.commands
             if command.startswith("cat ") and "/metrics.json " in command]
    assert len(reads) == 2
    assert all("/.polaris/runs/" in command for command in reads)


async def test_controller_runtime_source_mutation_invalidates_attractive_score(
    client, queue_stub, fake_ssh, bus_recorder,
):
    fake_ssh.run_logs = [metric_log(0.6), metric_log(0.99)]
    launches = 0
    mutated_path = None

    async def mutate_second_run_source(command):
        nonlocal launches, mutated_path
        if "nohup setsid bash " in command and "/operations/experiment-run/" in command:
            launches += 1
            if launches == 2:
                workspace = re.search(r"polaris_runs/[^/]+/\.polaris/runs/[^/]+", command)
                assert workspace is not None
                mutated_path = workspace.group() + "/train.py"
                fake_ssh.files[mutated_path] += "\n# changed during remote run\n"

    fake_ssh.on_command = mutate_second_run_source
    project_id, headers, exp_id, voyage_id = await _launch_experiment(
        client, budget={"max_hours": 2, "max_runs": 2},
    )
    await _drive_pipeline(client, headers, project_id, voyage_id,
                         _router_with(_ControllerProvider()))
    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"
    state = detail["iteration_state"]
    assert launches == 2 and mutated_path is not None
    assert "changed during remote run" in fake_ssh.files[mutated_path]
    assert state["incumbent"]["primary_value"] == 0.6
    assert state["no_improve_streak"] == 0
    bundle = state["result_bundle"]
    assert [e["seq"] for e in bundle["valid_evaluations"]] == [1]
    invalid = bundle["invalid_evaluations"][0]
    assert invalid["primary_value"] == 0.99
    assert "runtime_source_changed:train.py" in invalid["invalid_reasons"]
    assert "changed during remote run" not in state["candidates"][1]["files"]["train.py"]
    assert bundle["selected_candidate"]["files"] == state["candidates"][0]["files"]


class _EvidenceVerdictProvider(_ControllerProvider):
    def __init__(self, proposed_status):
        super().__init__(plan={"hypotheses": [{
            "text": "accuracy reaches 0.8", "status": "testing",
            "criterion": {"metric": "accuracy", "operator": ">=", "threshold": 0.8},
        }]})
        self.proposed_status = proposed_status
        self.observed_evaluations = []

    async def complete(
        self, messages, *, model, temperature=0.7, max_tokens=None, images=None,
    ):
        system = "\n".join(message.text for message in messages if message.role == "system")
        if not images and '"hypothesis_updates"' in system:
            user = "\n".join(message.text for message in messages if message.role == "user")
            line = next(line for line in user.splitlines()
                        if line.startswith("本轮有效性与证据引用："))
            evaluation = json.loads(line.split("：", 1)[1])
            self.observed_evaluations.append(evaluation)
            return _result(_reflection_json("improve", updates=[{
                "index": 0, "status": self.proposed_status, "evidence": "observed criterion",
                "evidence_refs": [evaluation["evaluation_id"]],
            }]), model)
        return await super().complete(
            messages, model=model, temperature=temperature, max_tokens=max_tokens, images=images,
        )


@pytest.mark.parametrize("score,status,verdict", [(0.9, "verified", "supported"),
                                                (0.7, "falsified", "refuted")])
async def test_controller_valid_evidence_refs_resolve_frozen_observational_criterion(
    client, queue_stub, fake_ssh, bus_recorder, score, status, verdict,
):
    fake_ssh.run_log = metric_log(score)
    provider = _EvidenceVerdictProvider(status)
    project_id, headers, exp_id, voyage_id = await _launch_experiment(client)
    await _drive_pipeline(client, headers, project_id, voyage_id, _router_with(provider))

    detail = await _get_detail(client, headers, exp_id)
    assert detail["status"] == "done"
    assert len(detail["runs"]) == 1
    assert provider.observed_evaluations[0]["valid"] is True
    state = detail["iteration_state"]
    assert state["stopped_reason"] == "hypotheses_resolved"
    assert detail["plan"]["hypotheses"][0]["status"] == status
    bundle = state["result_bundle"]
    assert bundle["scientific_outcome"] == "observational_criteria_resolved"
    scientific_verdict = bundle["scientific_verdicts"][0]
    assert scientific_verdict["verdict"] == verdict
    assert scientific_verdict["evidence_refs"] == [bundle["valid_evaluations"][0]["evaluation_id"]]
    assert scientific_verdict["criterion"] == {"metric": "accuracy", "operator": ">=",
                                               "threshold": 0.8}
    assert "causality and generalization unverified" in scientific_verdict["scope"]
    assert bundle["selected_evaluations"][0]["primary_value"] == score
