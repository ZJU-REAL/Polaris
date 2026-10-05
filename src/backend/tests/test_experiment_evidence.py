"""Offline regressions for selected-source lineage and observational evidence gates."""

import copy
import hashlib
import json

import pytest

from app.services.experiment_evidence import (
    apply_scientific_verdicts,
    build_result_bundle,
    condition_summary,
    freeze_research_contract,
    make_evaluation,
    promote_candidate,
    snapshot_candidate,
)


def contract(**extra):
    return freeze_research_contract(
        {"primary_metric": {"name": "accuracy", "direction": "maximize"},
         "hypotheses": [{"text": "accuracy reaches 0.9", "criterion": {
             "metric": "accuracy", "operator": ">=", "threshold": 0.9}}], **extra},
        objective="Compare implementations under one frozen protocol", budget={"max_runs": 3},
    )


def evaluation(c, candidate, score=0.9, *, run_id="1", **extra):
    selector = c["protocol"]["primary_metric"]["selector"]
    return make_evaluation(contract=c, candidate=candidate, run_id=run_id, seq=int(run_id),
                           status="succeeded", exit_code=0,
                           metrics={selector: [{"step": 0, "value": score}]},
                           primary_value=score, **extra)


def test_contract_frozen_fields_and_ids_ignore_mutable_status():
    plan = {"primary_metric": {"name": "loss", "direction": "minimize"},
            "hypotheses": [{"text": "same claim", "status": "testing"}],
            "datasets": [{"name": "fixed-data", "split": "development", "seed": 7}]}
    c = freeze_research_contract(plan)
    plan["hypotheses"][0]["status"] = "verified"
    assert freeze_research_contract(plan) == c
    plan["datasets"][0]["seed"] = 8
    assert freeze_research_contract(plan)["protocol_id"] != c["protocol_id"]
    assert c["protocol"]["datasets"][0]["seed"] == 7


@pytest.mark.parametrize("delta", [-1, float("nan"), float("inf"), True])
def test_contract_rejects_invalid_min_delta(delta):
    with pytest.raises(ValueError, match="min_delta"):
        contract(primary_metric={"name": "accuracy", "direction": "maximize", "min_delta": delta})


def test_candidate_snapshot_detaches_files_and_records_parent():
    files = {"train.py": "A"}
    a = snapshot_candidate(files)
    b = snapshot_candidate({"train.py": "B"}, parent_id=a["candidate_id"])
    files["train.py"] = "mutated"
    assert a["files"]["train.py"] == "A"
    assert b["parent_id"] == a["candidate_id"]
    assert b["source_digest"] != a["source_digest"]


def test_failed_run_with_high_score_cannot_promote():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    failed = make_evaluation(contract=c, candidate=a, run_id="1", seq=1, status="failed",
                             exit_code=1, metrics={"accuracy": [{"value": 0.99}]},
                             primary_value=0.99)
    state = promote_candidate({}, failed, a, c)
    assert failed["valid"] is False
    assert failed["metrics"]["accuracy"][0]["value"] == 0.99  # diagnostic retained
    assert "incumbent" not in state
    assert state["promotions"][0]["reason"] == "invalid_evaluation"


@pytest.mark.parametrize("score", [None, float("nan"), float("inf"), True])
def test_non_finite_primary_metric_is_invalid(score):
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    e = evaluation(c, a, score)
    assert not e["valid"]
    assert "missing_or_non_finite_primary_metric" in e["invalid_reasons"]


def test_old_or_foreign_result_identity_is_invalid():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    e = evaluation(c, a, identity={"candidate_id": "candidate:old", "protocol_id": "protocol:old"})
    assert not e["valid"]
    assert "candidate_id_mismatch" in e["invalid_reasons"]
    assert "protocol_id_mismatch" in e["invalid_reasons"]


def test_allowed_and_protected_source_gate():
    fixed = "independent evaluator source"
    expected = hashlib.sha256(fixed.encode()).hexdigest()
    c = contract(allowed_files=["train.py"], protected_files={"evaluate.py": expected})
    a = snapshot_candidate({"train.py": "A", "evaluate.py": fixed})
    assert evaluation(c, a)["valid"]
    changed = snapshot_candidate({"train.py": "A", "evaluate.py": "different evaluator"})
    assert "protected_file_changed:evaluate.py" in evaluation(c, changed)["invalid_reasons"]
    extra = snapshot_candidate({"train.py": "A", "evaluate.py": fixed, "data.py": "changed"})
    assert "files_outside_allowed_scope:data.py" in evaluation(c, extra)["invalid_reasons"]


def test_incumbent_keeps_best_source_after_two_worse_candidates():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    state = promote_candidate({"unrelated": {"keep": True}}, evaluation(c, a, 0.9), a, c)
    b = snapshot_candidate({"train.py": "B"}, parent_id=a["candidate_id"])
    state = promote_candidate(state, evaluation(c, b, 0.7, run_id="2"), b, c)
    newer = snapshot_candidate({"train.py": "C"}, parent_id=b["candidate_id"])
    state = promote_candidate(state, evaluation(c, newer, 0.6, run_id="3"), newer, c)
    assert state["incumbent"]["candidate_id"] == a["candidate_id"]
    assert state["working_candidate_id"] == newer["candidate_id"]
    assert [p["accepted"] for p in state["promotions"]] == [True, False, False]
    assert state["unrelated"] == {"keep": True}
    bundle = build_result_bundle(experiment_id="experiment:1", contract=c,
                                 candidates=state["candidates"], evaluations=state["evaluations"],
                                 selected_candidate_id=state["incumbent"]["candidate_id"])
    assert bundle["selected_candidate"]["files"]["train.py"] == "A"
    assert bundle["selected_evaluations"][0]["primary_value"] == 0.9


def test_minimize_min_delta_and_idempotent_replay():
    c = contract(primary_metric={"name": "loss", "direction": "minimize", "min_delta": 0.1})
    a = snapshot_candidate({"train.py": "A"})
    first = evaluation(c, a, 2.0)
    state = promote_candidate({}, first, a, c)
    assert promote_candidate(state, first, a, c) == state
    b = snapshot_candidate({"train.py": "B"})
    state = promote_candidate(state, evaluation(c, b, 1.95, run_id="2"), b, c)
    assert not state["promotions"][-1]["accepted"]
    state = promote_candidate(state, evaluation(c, b, 1.7, run_id="3"), b, c)
    assert state["incumbent"]["primary_value"] == 1.7


def test_protocol_change_does_not_compare_old_incumbent():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    state = promote_candidate({}, evaluation(c, a), a, c)
    revised = contract(datasets=[{"name": "new-data"}])
    state = promote_candidate(state, evaluation(revised, a, 0.99, run_id="2"), a, revised)
    assert state["promotions"][-1]["reason"] == "incumbent_protocol_mismatch"
    assert state["incumbent"]["protocol_id"] == c["protocol_id"]


def test_condition_comparison_requires_same_context_in_this_evaluation():
    c = contract(conditions=[{"name": "control", "role": "baseline"},
                             {"name": "method", "role": "treatment"}])
    metrics = {"accuracy/model-a/control": [{"value": 0.4}],
               "accuracy/model-a/method": [{"value": 0.7}]}
    assert condition_summary(metrics, c)["deltas_vs_baseline"] == {"method": pytest.approx(0.3)}
    metrics["accuracy/model-b/method"] = [{"value": 0.8}]
    assert condition_summary(metrics, c) is None  # cannot borrow model-b's historic baseline
    a = snapshot_candidate({"train.py": "A"})
    e = make_evaluation(contract=c, candidate=a, run_id="1", seq=1, status="succeeded",
                        exit_code=0, metrics=metrics, primary_value=0.7)
    assert "missing_or_unpaired_conditions" in e["invalid_reasons"]


def test_empty_refs_and_arbitrary_hypothesis_cannot_be_verified():
    c = contract()
    proposed = [{"index": 0, "status": "verified", "evidence": "Looks better"}]
    plan, verdicts = apply_scientific_verdicts(
        {"hypotheses": [{"text": "accuracy reaches 0.9"}]}, proposed,
                                             contract=c, evaluations=[])
    assert plan["hypotheses"][0]["status"] == "testing"
    assert verdicts[0]["verdict"] == "inconclusive"
    a = snapshot_candidate({"train.py": "A"})
    e = evaluation(c, a)
    c = contract(hypotheses=[{"text": "A proves a broad causal mechanism"}])
    plan["hypotheses"][0]["text"] = c["claims"][0]["text"]
    proposed[0]["evidence_refs"] = [e["evaluation_id"]]
    _, verdicts = apply_scientific_verdicts(plan, proposed, contract=c, evaluations=[e])
    assert verdicts[0]["reason"] == "missing_explicit_criterion"


@pytest.mark.parametrize("score,status,verdict", [(0.95, "verified", "supported"),
                                                   (0.8, "falsified", "refuted")])
def test_explicit_valid_evidence_can_support_or_refute_limited_criterion(score, status, verdict):
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    e = evaluation(c, a, score)
    plan, verdicts = apply_scientific_verdicts(
        {"hypotheses": [{"text": "accuracy reaches 0.9"}]},
        [{"index": 0, "status": status, "evidence_refs": [e["evaluation_id"]]}],
        contract=c, evaluations=[e],
    )
    assert verdicts[0]["verdict"] == verdict
    assert plan["hypotheses"][0]["status"] == status
    assert "generalization unverified" in verdicts[0]["scope"]


def test_invalid_ref_and_contradictory_evidence_remain_inconclusive():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    good = evaluation(c, a, 0.95)
    bad = evaluation(c, a, 0.8, run_id="2")
    update = {"index": 0, "status": "verified", "evidence_refs": ["run:missing"]}
    plan = {"hypotheses": [{"text": "accuracy reaches 0.9"}]}
    _, verdicts = apply_scientific_verdicts(plan, [update], contract=c, evaluations=[good, bad])
    assert verdicts[0]["reason"] == "invalid_or_foreign_evidence"
    update["evidence_refs"] = [good["evaluation_id"], bad["evaluation_id"]]
    _, verdicts = apply_scientific_verdicts(plan, [update], contract=c, evaluations=[good, bad])
    assert verdicts[0]["verdict"] == "inconclusive"
    assert verdicts[0]["reason"] == "conflicting_evidence"


def test_bundle_digest_changes_when_evidence_changes_and_rejects_tampered_source():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    e = evaluation(c, a)
    kwargs = {"experiment_id": "1", "contract": c, "candidates": [a], "evaluations": [e],
              "selected_candidate_id": a["candidate_id"]}
    bundle = build_result_bundle(**kwargs)
    e["metrics"]["accuracy"][0]["value"] = 0.91
    assert build_result_bundle(**kwargs)["bundle_id"] != bundle["bundle_id"]
    changed = copy.deepcopy(a)
    changed["files"]["train.py"] = "later working source"
    kwargs["candidates"] = [changed]
    assert build_result_bundle(**kwargs)["selected_candidate"] is None


@pytest.mark.parametrize("selector", [None, "", " ", {"mean": "accuracy"}, True])
def test_selector_must_be_an_exact_nonempty_metric_name(selector):
    with pytest.raises(ValueError, match="selector"):
        contract(primary_metric={"name": "accuracy", "direction": "maximize", "selector": selector})


def test_exact_selector_uses_final_point_and_rejects_supplied_score_mismatch():
    c = contract(primary_metric={"name": "accuracy", "direction": "maximize",
                                "selector": "accuracy/model-a/method"})
    a = snapshot_candidate({"train.py": "A"})
    metrics = {"accuracy/model-a/method": [{"value": 0.7}, {"value": 0.8}],
               "accuracy": [{"value": 0.99}]}
    e = make_evaluation(contract=c, candidate=a, run_id="1", seq=1, status="succeeded",
                        exit_code=0, metrics=metrics, primary_value=0.99)
    assert not e["valid"]
    assert "primary_metric_value_mismatch" in e["invalid_reasons"]
    e = make_evaluation(contract=c, candidate=a, run_id="1", seq=1, status="succeeded",
                        exit_code=0, metrics=metrics, primary_value=0.8)
    assert e["valid"]
    metrics.pop("accuracy/model-a/method")
    e = make_evaluation(contract=c, candidate=a, run_id="1", seq=1, status="succeeded",
                        exit_code=0, metrics=metrics, primary_value=0.99)
    assert "missing_exact_primary_metric" in e["invalid_reasons"]


def test_protected_paths_survive_planning_before_baseline_source_exists():
    plan = {"primary_metric": {"name": "accuracy", "direction": "maximize"},
            "allowed_files": ["train.py"], "protected_files": ["evaluate.py", "dataset.py"]}
    pending = freeze_research_contract(plan)
    assert pending["protocol"]["protected_file_paths"] == ["dataset.py", "evaluate.py"]
    assert pending["protocol"]["protected_files"] == {}
    files = {"train.py": "A", "evaluate.py": "fixed evaluator", "dataset.py": "fixed data"}
    a = snapshot_candidate(files)
    e = evaluation(pending, a)
    assert not e["valid"]
    assert "protected_file_not_frozen:evaluate.py" in e["invalid_reasons"]
    assert not any(reason.startswith("files_outside_allowed_scope")
                   for reason in e["invalid_reasons"])
    final = freeze_research_contract(plan, initial_files=files)
    assert final["protocol_id"] != pending["protocol_id"]
    assert evaluation(final, a)["valid"]
    partial = freeze_research_contract(plan, initial_files={"evaluate.py": files["evaluate.py"]})
    assert "protected_file_not_frozen:dataset.py" in evaluation(partial, a)["invalid_reasons"]


@pytest.mark.parametrize("count", [None, 99, 101, 0, -1, 100.5, True, float("nan")])
def test_explicit_sample_count_rejects_partial_or_invalid_results(count):
    c = contract(eval_protocol={"sample_count_metric": "evaluated_examples", "n_examples": 100})
    a = snapshot_candidate({"train.py": "A"})
    metrics = {"accuracy": [{"value": 0.9}]}
    if count is not None:
        metrics["evaluated_examples"] = [{"value": count}]
    e = make_evaluation(contract=c, candidate=a, run_id="1", seq=1, status="succeeded",
                        exit_code=0, metrics=metrics, primary_value=0.9)
    assert not e["valid"]
    assert any(reason in e["invalid_reasons"] for reason in (
        "invalid_or_missing_sample_count", "incomplete_sample_count",
    ))
    assert "incumbent" not in promote_candidate({}, e, a, c)


def test_complete_sample_count_and_required_count_selector():
    a = snapshot_candidate({"train.py": "A"})
    c = contract(eval_protocol={"n_examples": 100})
    assert "missing_sample_count_metric" in evaluation(c, a)["invalid_reasons"]
    c = contract(eval_protocol={"sample_count_metric": "evaluated_examples", "n_examples": 100})
    metrics = {"accuracy": [{"value": 0.9}], "evaluated_examples": [{"value": 100}]}
    e = make_evaluation(contract=c, candidate=a, run_id="1", seq=1, status="succeeded",
                        exit_code=0, metrics=metrics, primary_value=0.9)
    assert e["valid"]


@pytest.mark.parametrize("count", [0, -1, True, 1.5, "100"])
def test_declared_sample_target_must_be_a_positive_integer(count):
    c = contract(eval_protocol={"sample_count_metric": "evaluated_examples", "n_examples": count})
    a = snapshot_candidate({"train.py": "A"})
    e = make_evaluation(contract=c, candidate=a, run_id="1", seq=1, status="succeeded",
                        exit_code=0, metrics={"accuracy": [{"value": 0.9}],
                                              "evaluated_examples": [{"value": 100}]},
                        primary_value=0.9)
    assert "invalid_expected_sample_count" in e["invalid_reasons"]


@pytest.mark.parametrize("field,value", [("candidate_id", "candidate:forged"),
                                         ("source_digest", "forged"),
                                         ("parent_id", "candidate:new-parent"),
                                         ("environment_digest", "forged")])
def test_candidate_source_and_lineage_identity_cannot_be_forged(field, value):
    c = contract()
    a = snapshot_candidate({"train.py": "A"}, environment={"container": "fixed-image"})
    a[field] = value
    e = evaluation(c, a)
    assert not e["valid"]
    assert "candidate_identity_mismatch" in e["invalid_reasons"]
    assert "incumbent" not in promote_candidate({}, e, a, c)


def test_frozen_protocol_tamper_cannot_be_promoted_or_verify_claim():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    e = evaluation(c, a)
    c["protocol"]["primary_metric"]["direction"] = "minimize"
    assert "contract_identity_mismatch" in evaluation(c, a)["invalid_reasons"]
    assert "incumbent" not in promote_candidate({}, e, a, c)
    _, verdicts = apply_scientific_verdicts(
        {"hypotheses": [{"text": "accuracy reaches 0.9"}]},
        [{"index": 0, "status": "verified", "evidence_refs": [e["evaluation_id"]]}],
        contract=c, evaluations=[e],
    )
    assert verdicts[0]["reason"] == "contract_identity_mismatch"


def test_stale_contract_and_conflicting_replay_do_not_move_working_or_incumbent():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    first = evaluation(c, a)
    state = promote_candidate({"research_contract": c}, first, a, c)
    b = snapshot_candidate({"train.py": "B"})
    replay = evaluation(c, b, 0.99)
    assert promote_candidate(state, replay, b, c) == state
    revised = contract(datasets=[{"name": "different-data"}])
    stale = promote_candidate(state, evaluation(revised, b, 0.99, run_id="2"), b, revised)
    assert stale["working_candidate_id"] == a["candidate_id"]
    assert stale["incumbent"] == state["incumbent"]
    assert stale["promotions"][-1]["reason"] == "stale_contract"


def test_incumbent_numeric_value_cannot_be_detached_from_accepted_evidence():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    state = promote_candidate({}, evaluation(c, a), a, c)
    state["incumbent"]["primary_value"] = 0.1
    b = snapshot_candidate({"train.py": "B"})
    state = promote_candidate(state, evaluation(c, b, 0.5, run_id="2"), b, c)
    assert state["promotions"][-1]["reason"] == "incumbent_evidence_mismatch"


@pytest.mark.parametrize("mutation", ["metrics", "status", "identity"])
def test_verdict_gate_rechecks_persisted_evaluation_instead_of_trusting_valid_flag(mutation):
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    e = evaluation(c, a)
    if mutation == "metrics":
        e["metrics"]["accuracy"][-1]["value"] = 0.99
    elif mutation == "status":
        e["status"] = "failed"
    else:
        e["source_identity"] = {"candidate_id": "candidate:old"}
    _, verdicts = apply_scientific_verdicts(
        {"hypotheses": [{"text": "accuracy reaches 0.9"}]},
        [{"index": 0, "status": "verified", "evidence_refs": [e["evaluation_id"]]}],
        contract=c, evaluations=[e],
    )
    assert verdicts[0]["verdict"] == "inconclusive"
    assert verdicts[0]["reason"] == "invalid_or_foreign_evidence"


def test_stale_claim_and_ambiguous_run_refs_cannot_verify_a_different_question():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    e = evaluation(c, a)
    updates = [{"index": 0, "status": "verified", "evidence_refs": [e["evaluation_id"]]}]
    _, verdicts = apply_scientific_verdicts(
        {"hypotheses": [{"text": "A improves generalization"}]}, updates,
        contract=c, evaluations=[e],
    )
    assert verdicts[0]["reason"] == "claim_identity_mismatch"
    conflicting = evaluation(c, a, 0.95)
    _, verdicts = apply_scientific_verdicts(
        {"hypotheses": [{"text": "accuracy reaches 0.9"}]}, updates,
        contract=c, evaluations=[e, conflicting],
    )
    assert verdicts[0]["reason"] == "invalid_or_foreign_evidence"


def test_result_bundle_contains_reproduction_logs_environment_and_failed_diagnostics():
    c = contract()
    a = snapshot_candidate({"train.py": "A"}, environment={"container": "fixed-image"})
    e = evaluation(c, a, command="bash run.sh", log_path="run-1.log",
                   job_identity={"job_id": "remote-job-1", "process_id": 123})
    failed = make_evaluation(
        contract=c, candidate=a, run_id="2", seq=2, status="failed", exit_code=1,
        metrics={"accuracy": [{"value": float("nan")}]}, primary_value=None,
        command="bash run.sh", log_path="run-2.log", job_identity={"job_id": "remote-job-2"},
    )
    bundle = build_result_bundle(experiment_id="1", contract=c, candidates=[a],
                                 evaluations=[e, failed], selected_candidate_id=a["candidate_id"])
    assert bundle["selected_candidate"]["environment"] == {"container": "fixed-image"}
    assert bundle["reproduction"][0]["command"] == "bash run.sh"
    assert bundle["reproduction"][0]["job_identity"]["process_id"] == 123
    assert {entry["log_path"] for entry in bundle["log_references"]} == {"run-1.log", "run-2.log"}
    assert bundle["failed_diagnostics"][0]["exit_code"] == 1
    assert bundle["failed_diagnostics"][0]["metrics"]["accuracy"][0]["value"] == {
        "invalid_numeric_value": "nan",
    }
    json.dumps(bundle, allow_nan=False)


def test_bundle_excludes_valid_flag_with_missing_or_foreign_source():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    e = evaluation(c, a)
    e["source_digest"] = "source-of-another-candidate"
    bundle = build_result_bundle(experiment_id="1", contract=c, candidates=[a],
                                 evaluations=[e], selected_candidate_id=a["candidate_id"])
    assert not bundle["valid_evaluations"]
    assert bundle["selected_candidate"] is None
    assert "candidate_source_unavailable" in bundle["invalid_evaluations"][0]["invalid_reasons"]


def test_external_protected_source_does_not_hide_changed_candidate_evaluator():
    fixed = "fixed evaluator"
    c = contract(protected_files={"evaluate.py": hashlib.sha256(fixed.encode()).hexdigest()})
    a = snapshot_candidate({"train.py": "A", "evaluate.py": "candidate edited evaluator"})
    e = evaluation(c, a, protected_sources={"evaluate.py": fixed})
    assert "protected_file_changed:evaluate.py" in e["invalid_reasons"]


def test_bundle_revalidates_cached_verdict_when_original_evaluation_changes():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    e = evaluation(c, a)
    _, verdicts = apply_scientific_verdicts(
        {"hypotheses": [{"text": "accuracy reaches 0.9"}]},
        [{"index": 0, "status": "verified", "evidence_refs": [e["evaluation_id"]]}],
        contract=c, evaluations=[e],
    )
    kwargs = {"experiment_id": "1", "contract": c, "candidates": [a], "evaluations": [e],
              "selected_candidate_id": a["candidate_id"], "verdicts": verdicts}
    assert build_result_bundle(**kwargs)["scientific_verdicts"][0]["verdict"] == "supported"
    e["metrics"]["accuracy"][-1]["value"] = 0.7
    bundle = build_result_bundle(**kwargs)
    assert bundle["selected_candidate"] is None
    assert bundle["scientific_verdicts"][0]["verdict"] == "inconclusive"
    assert bundle["scientific_verdicts"][0]["reason"] == "invalid_or_foreign_evidence"


def test_bundle_rejects_conflicting_replay_and_deduplicates_identical_evaluation():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    e = evaluation(c, a)
    bundle = build_result_bundle(experiment_id="1", contract=c, candidates=[a],
                                 evaluations=[e, copy.deepcopy(e)],
                                 selected_candidate_id=a["candidate_id"])
    assert len(bundle["valid_evaluations"]) == 1
    other = evaluation(c, a, 0.99)
    bundle = build_result_bundle(experiment_id="1", contract=c, candidates=[a],
                                 evaluations=[e, other], selected_candidate_id=a["candidate_id"])
    assert not bundle["valid_evaluations"]
    assert bundle["selected_candidate"] is None
    assert "conflicting_evaluation_replay" in bundle["invalid_evaluations"][0]["invalid_reasons"]


def test_nonfinite_earlier_metric_is_retained_as_invalid_diagnostic():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    e = make_evaluation(contract=c, candidate=a, run_id="1", seq=1, status="succeeded",
                        exit_code=0, metrics={"accuracy": [{"value": float("inf")},
                                                           {"value": 0.9}]}, primary_value=0.9)
    assert "invalid_metric:accuracy" in e["invalid_reasons"]
    bundle = build_result_bundle(experiment_id="1", contract=c, candidates=[a],
                                 evaluations=[e], selected_candidate_id=a["candidate_id"])
    assert bundle["scientific_outcome"] == "inconclusive"
    json.dumps(bundle, allow_nan=False)


def test_valid_negative_result_can_complete_observational_criteria():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    e = evaluation(c, a, 0.7)
    _, verdicts = apply_scientific_verdicts(
        {"hypotheses": [{"text": "accuracy reaches 0.9"}]},
        [{"index": 0, "status": "falsified", "evidence_refs": [e["evaluation_id"]]}],
        contract=c, evaluations=[e],
    )
    bundle = build_result_bundle(experiment_id="1", contract=c, candidates=[a],
                                 evaluations=[e], selected_candidate_id=a["candidate_id"],
                                 verdicts=verdicts, stopped_reason="hypotheses_resolved")
    assert bundle["scientific_outcome"] == "observational_criteria_resolved"
    assert bundle["scientific_verdicts"][0]["verdict"] == "refuted"
    assert bundle["optimization_outcome"] == "selected"


def test_cached_valid_flag_cannot_override_unfrozen_or_changed_protected_source():
    a = snapshot_candidate({"train.py": "A", "evaluate.py": "fixed evaluator"})
    pending = contract(protected_files=["evaluate.py"])
    e = evaluation(pending, a)
    e.update({"valid": True, "invalid_reasons": []})
    assert "incumbent" not in promote_candidate({}, e, a, pending)
    frozen = freeze_research_contract(
        {"primary_metric": {"name": "accuracy", "direction": "maximize"},
         "protected_files": ["evaluate.py"]}, initial_files=a["files"],
    )
    e = evaluation(frozen, a)
    assert e["valid"]
    e["protected_source_digests"]["evaluate.py"] = "different source"
    assert "incumbent" not in promote_candidate({}, e, a, frozen)


def test_promotion_rechecks_incumbent_source_snapshot_before_comparing():
    c = contract()
    a = snapshot_candidate({"train.py": "A"})
    state = promote_candidate({}, evaluation(c, a), a, c)
    state["candidates"][0]["files"]["train.py"] = "overwritten history"
    b = snapshot_candidate({"train.py": "B"})
    state = promote_candidate(state, evaluation(c, b, 0.95, run_id="2"), b, c)
    assert state["promotions"][-1]["reason"] == "incumbent_evidence_mismatch"


def test_original_objective_details_are_immutable_and_bind_contract_identity():
    source = {"idea_id": "idea-1", "title": "original",
              "goal": {"question": "Original question", "objectives": ["criterion"]}}
    plan = {"primary_metric": {"name": "accuracy", "direction": "maximize"}}
    original = freeze_research_contract(plan, objective_details=source)
    source["goal"]["question"] = "Changed question"
    changed = freeze_research_contract(plan, objective_details=source)
    assert original["objective_details"]["goal"]["question"] == "Original question"
    assert original["protocol_id"] == changed["protocol_id"]
    assert original["contract_id"] != changed["contract_id"]
