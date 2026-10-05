"""Deterministic research identities, candidate selection, and evidence lineage.

These JSON records live in Experiment.iteration_state; they need no migration.
A model proposes a verdict, but only a frozen, explicit numerical criterion and
valid referenced evaluations can publish an observational verdict. This module
never infers a causal or generalization claim from an improved score.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any

SCHEMA_VERSION = 1


def _digest(value: Any) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _last(series: Any) -> float | None:
    if not isinstance(series, list) or not series or not isinstance(series[-1], dict):
        return None
    return _number(series[-1].get("value"))


def _valid_series(series: Any) -> bool:
    return bool(isinstance(series, list) and series and all(
        isinstance(point, dict) and _number(point.get("value")) is not None for point in series
    ))


def _source_files(files: Any) -> bool:
    return bool(isinstance(files, dict) and files and all(
        isinstance(path, str) and path and isinstance(content, str)
        for path, content in files.items()
    ))


def _contract_matches_identity(contract: dict[str, Any]) -> bool:
    try:
        return bool(
            contract.get("protocol_id") == "protocol:" + _digest(contract.get("protocol"))
            and contract.get("contract_id") == "contract:" + _digest({
                key: value for key, value in contract.items() if key != "contract_id"
            })
        )
    except (TypeError, ValueError):
        return False


def _candidate_matches_identity(candidate: dict[str, Any]) -> bool:
    files = candidate.get("files")
    if not _source_files(files):
        return False
    try:
        return bool(
            candidate.get("source_digest") == _digest(files)
            and ("environment" not in candidate
                 or candidate.get("environment_digest") == _digest(candidate["environment"]))
            and candidate.get("candidate_id") == "candidate:" + _digest({
                key: value for key, value in candidate.items() if key != "candidate_id"
            })
        )
    except (TypeError, ValueError):
        return False


def freeze_research_contract(
    plan: dict[str, Any], *, objective: str = "", budget: dict[str, Any] | None = None,
    initial_files: dict[str, str] | None = None,
    objective_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Freeze comparability fields, retaining unresolved protected paths.

    A protocol change requires a new contract and baseline. Before the first
    evaluation, declared protected paths must be frozen from their initial
    sources. A digest is an identity, not proof of independent evaluator/data
    isolation.
    """
    pm = plan.get("primary_metric") or {}
    name = str(pm.get("name") or "").strip()
    direction = pm.get("direction")
    selector = pm.get("selector", name)
    min_delta = _number(pm.get("min_delta", 0))
    if not name or direction not in {"maximize", "minimize"}:
        raise ValueError("research contract requires an exact metric and direction")
    if not isinstance(selector, str) or not selector.strip():
        raise ValueError("primary_metric.selector must be an exact non-empty metric name")
    if min_delta is None or min_delta < 0:
        raise ValueError("primary_metric.min_delta must be finite and non-negative")
    protected = plan.get("protected_files") or {}
    if isinstance(protected, list):
        if any(not isinstance(path, str) or not path for path in protected):
            raise ValueError("protected_files paths must be non-empty strings")
        protected_paths = sorted(set(protected))
        protected = {
            path: hashlib.sha256(initial_files[path].encode()).hexdigest()
            for path in protected_paths
            if initial_files and isinstance(initial_files.get(path), str)
        }
    elif isinstance(protected, dict):
        if any(not isinstance(path, str) or not path or not isinstance(digest, str)
               or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest)
               for path, digest in protected.items()):
            raise ValueError("protected_files must map paths to SHA-256 digests")
        protected_paths = sorted(protected)
    else:
        raise ValueError("protected_files must be a list or digest mapping")
    protocol = {
        "primary_metric": {"name": name, "direction": direction, "min_delta": min_delta,
                           "selector": selector.strip()},
        "conditions": copy.deepcopy(plan.get("conditions") or []),
        "datasets": copy.deepcopy(plan.get("datasets") or []),
        "eval_protocol": copy.deepcopy(plan.get("eval_protocol") or {}),
        "evaluator_digest": plan.get("evaluator_digest"),
        "allowed_files": copy.deepcopy(plan.get("allowed_files")),
        "protected_file_paths": protected_paths,
        "protected_files": copy.deepcopy(protected),
    }
    claims = []
    for index, hyp in enumerate(plan.get("hypotheses") or []):
        if not isinstance(hyp, dict):
            continue
        text = str(hyp.get("text") or "")
        criterion = copy.deepcopy(hyp.get("criterion"))
        claims.append({
            "claim_id": "claim:" + _digest({"index": index, "text": text, "criterion": criterion}),
            "index": index, "text": text, "criterion": criterion,
        })
    contract = {"schema_version": SCHEMA_VERSION, "objective": objective,
                "protocol": protocol, "claims": claims, "budget": copy.deepcopy(budget or {})}
    if objective_details is not None:
        contract["objective_details"] = copy.deepcopy(objective_details)
    contract["protocol_id"] = "protocol:" + _digest(protocol)
    contract["contract_id"] = "contract:" + _digest(contract)
    return contract


def snapshot_candidate(
    files: dict[str, str], *, parent_id: str | None = None, change_type: str = "method",
    environment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not _source_files(files):
        raise ValueError("candidate requires a non-empty source snapshot")
    source = copy.deepcopy(files)
    environment_snapshot = copy.deepcopy(environment or {})
    candidate = {"files": source, "source_digest": _digest(source), "parent_id": parent_id,
                 "change_type": change_type, "environment": environment_snapshot,
                 "environment_digest": _digest(environment_snapshot)}
    candidate["candidate_id"] = "candidate:" + _digest(candidate)
    return candidate


def _condition_values(metrics: dict[str, Any], metric: str, condition: str) -> dict[str, float]:
    values = {}
    for key, series in metrics.items():
        # Exact path components prevent similarly named metrics or conditions
        # from satisfying the frozen selector.
        if not isinstance(key, str) or not key.startswith(metric + "/"):
            continue
        parts = key[len(metric) + 1:].split("/")
        if not parts or parts[-1] != condition:
            continue
        value = _last(series)
        if value is not None:
            values["/".join(parts[:-1])] = value
    return values


def condition_summary(metrics: dict[str, Any], contract: dict[str, Any]) -> dict[str, Any] | None:
    """Return same-evaluation, paired contexts; absent/unpaired values never mix."""
    protocol = contract.get("protocol") or {}
    conditions = protocol.get("conditions") or []
    if any(not isinstance(condition, dict) or not isinstance(condition.get("name"), str)
           or not condition["name"] for condition in conditions):
        return None
    names = [condition["name"] for condition in conditions]
    if len(names) != len(set(names)):
        return None
    baselines = [c["name"] for c in conditions if c.get("role") == "baseline"]
    treatments = [c["name"] for c in conditions if c.get("role") == "treatment"]
    if len(baselines) != 1 or not treatments:
        return None
    metric = str((protocol.get("primary_metric") or {}).get("name") or "")
    baseline = baselines[0]
    values = {name: _condition_values(metrics, metric, name)
              for name in [baseline, *treatments]}
    contexts = set(values[baseline])
    if not contexts or any(set(points) != contexts for points in values.values()):
        return None
    scores = {name: sum(value / len(points) for value in points.values())
              for name, points in values.items()}
    sign = -1 if protocol["primary_metric"].get("direction") == "minimize" else 1
    deltas = {name: scores[name] - scores[baseline] for name in treatments}
    if any(_number(value) is None for value in [*scores.values(), *deltas.values()]):
        return None
    return {"baseline": baseline, "scores": scores, "contexts": sorted(contexts),
            "deltas_vs_baseline": deltas,
            "improvements_vs_baseline": {name: sign * value for name, value in deltas.items()},
            "paired_values": values}


def _metric_validation_reasons(
    metrics: Any, primary_value: Any, contract: dict[str, Any],
) -> list[str]:
    reasons = []
    protocol = contract.get("protocol") or {}
    pm = protocol.get("primary_metric") or {}
    selector = pm.get("selector", pm.get("name"))
    if not isinstance(metrics, dict):
        metrics = {}
        reasons.append("invalid_metrics_record")
    value = _number(primary_value)
    if value is None:
        reasons.append("missing_or_non_finite_primary_metric")
    selected_value = _last(metrics.get(selector)) if isinstance(selector, str) else None
    if selected_value is None:
        reasons.append("missing_exact_primary_metric")
    elif value is not None and value != selected_value:
        reasons.append("primary_metric_value_mismatch")
    for key, series in metrics.items():
        if not isinstance(key, str) or not _valid_series(series):
            reasons.append("invalid_metric:" + str(key))
    eval_protocol = protocol.get("eval_protocol") or {}
    count_selector = eval_protocol.get("sample_count_metric")
    expected_count = eval_protocol.get("n_examples")
    if count_selector is not None or expected_count is not None:
        if not isinstance(count_selector, str) or not count_selector.strip():
            reasons.append("missing_sample_count_metric")
        else:
            count = _last(metrics.get(count_selector))
            if count is None or count < 1 or not count.is_integer():
                reasons.append("invalid_or_missing_sample_count")
            if expected_count is not None:
                expected = _number(expected_count)
                if expected is None or expected < 1 or not expected.is_integer():
                    reasons.append("invalid_expected_sample_count")
                elif count is not None and count != expected:
                    reasons.append("incomplete_sample_count")
    if protocol.get("conditions") and condition_summary(metrics, contract) is None:
        reasons.append("missing_or_unpaired_conditions")
    return reasons


def make_evaluation(
    *, contract: dict[str, Any], candidate: dict[str, Any], run_id: str, seq: int,
    status: str, exit_code: int | None, metrics: dict[str, Any] | None,
    primary_value: float | None, identity: dict[str, Any] | None = None,
    protected_sources: dict[str, str] | None = None, command: str | None = None,
    log_path: str | None = None, job_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep partial results for diagnosis; only valid results enter selection."""
    reasons = []
    protocol = contract.get("protocol") or {}
    candidate_id = candidate.get("candidate_id")
    if not _contract_matches_identity(contract):
        reasons.append("contract_identity_mismatch")
    if not _candidate_matches_identity(candidate):
        reasons.append("candidate_identity_mismatch")
    if not isinstance(run_id, str) or not run_id.strip():
        reasons.append("missing_run_identity")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        reasons.append("invalid_run_sequence")
    if status != "succeeded" or exit_code != 0:
        reasons.append("run_not_successful")
    metrics_copy = copy.deepcopy(metrics if isinstance(metrics, dict) else {})
    reasons.extend(_metric_validation_reasons(metrics, primary_value, contract))
    for key, expected in (("candidate_id", candidate_id),
                          ("protocol_id", contract.get("protocol_id")),
                          ("contract_id", contract.get("contract_id"))):
        if identity is not None and identity.get(key) != expected:
            reasons.append(key + "_mismatch")
    files = candidate.get("files") or {}
    allowed = protocol.get("allowed_files")
    protected = protocol.get("protected_files") or {}
    protected_paths = set(protocol.get("protected_file_paths") or []) | set(protected)
    protected_source_digests = {}
    if isinstance(allowed, list):
        unexpected = set(files) - set(allowed) - protected_paths
        if unexpected:
            reasons.append("files_outside_allowed_scope:" + ",".join(sorted(unexpected)))
    for path in sorted(protected_paths):
        expected = protected.get(path)
        if not expected:
            reasons.append("protected_file_not_frozen:" + path)
            continue
        content = (protected_sources or {}).get(path, files.get(path))
        if isinstance(content, str):
            protected_source_digests[path] = hashlib.sha256(content.encode()).hexdigest()
        if (not isinstance(content, str)
                or hashlib.sha256(content.encode()).hexdigest() != expected
                or (path in files and (
                    not isinstance(files[path], str)
                    or hashlib.sha256(files[path].encode()).hexdigest() != expected
                ))):
            reasons.append("protected_file_changed:" + path)
    return {"schema_version": SCHEMA_VERSION, "evaluation_id": "run:" + str(run_id),
            "run_id": str(run_id), "seq": seq, "candidate_id": candidate_id,
            "source_digest": candidate.get("source_digest"),
            "environment_digest": candidate.get("environment_digest"),
            "protected_source_digests": protected_source_digests,
            "contract_id": contract.get("contract_id"), "protocol_id": contract.get("protocol_id"),
            "source_identity": copy.deepcopy(identity), "command": command, "log_path": log_path,
            "job_identity": copy.deepcopy(job_identity), "status": status, "exit_code": exit_code,
            "metrics": metrics_copy, "primary_value": _number(primary_value),
            "valid": not reasons, "invalid_reasons": list(dict.fromkeys(reasons))}


def _valid_for_contract(evaluation: dict[str, Any], contract: dict[str, Any]) -> bool:
    run_id = evaluation.get("run_id")
    seq = evaluation.get("seq")
    identity = evaluation.get("source_identity")
    protocol = contract.get("protocol") or {}
    protected = protocol.get("protected_files") or {}
    protected_paths = set(protocol.get("protected_file_paths") or []) | set(protected)
    protected_sources = evaluation.get("protected_source_digests") or {}
    return bool(
        _contract_matches_identity(contract)
        and evaluation.get("valid") is True and not evaluation.get("invalid_reasons")
        and isinstance(run_id, str) and run_id.strip()
        and isinstance(seq, int) and not isinstance(seq, bool) and seq > 0
        and evaluation.get("evaluation_id") == "run:" + run_id
        and evaluation.get("contract_id") == contract.get("contract_id")
        and evaluation.get("protocol_id") == contract.get("protocol_id")
        and evaluation.get("status") == "succeeded" and evaluation.get("exit_code") == 0
        and all(protected.get(path) and protected_sources.get(path) == protected[path]
                for path in protected_paths)
        and not _metric_validation_reasons(
            evaluation.get("metrics"), evaluation.get("primary_value"), contract,
        )
        and (identity is None or all(identity.get(key) == evaluation.get(key)
                                     for key in ("candidate_id", "contract_id", "protocol_id")))
    )


def _evaluation_matches_candidate(evaluation: dict[str, Any], candidate: dict[str, Any]) -> bool:
    return bool(_candidate_matches_identity(candidate) and all(
        evaluation.get(key) == candidate.get(key)
        for key in ("candidate_id", "source_digest", "environment_digest")
    ) and all(
        path not in candidate["files"]
        or hashlib.sha256(candidate["files"][path].encode()).hexdigest() == digest
        for path, digest in (evaluation.get("protected_source_digests") or {}).items()
    ))


def promote_candidate(
    state: dict[str, Any], evaluation: dict[str, Any], candidate: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Select an incumbent under one protocol, preserving explored sources.

    The experiment's Voyage execution lease serializes this JSON publication.
    This function does not replace database fencing required across workers.
    Replays cannot change an existing run, or move its working/delivery pointers.
    """
    out = copy.deepcopy(state)
    evaluations = list(out.get("evaluations") or [])
    if any(e.get("evaluation_id") == evaluation["evaluation_id"] for e in evaluations):
        return out
    candidates = list(out.get("candidates") or [])
    if not any(c.get("candidate_id") == candidate["candidate_id"] for c in candidates):
        candidates.append(copy.deepcopy(candidate))
    out["candidates"] = candidates
    out["evaluations"] = [*evaluations, copy.deepcopy(evaluation)]
    incumbent = out.get("incumbent") or {}
    accepted = False
    reason = "invalid_evaluation"
    pm = contract["protocol"]["primary_metric"]
    state_contract = out.get("research_contract")
    if state_contract and (state_contract.get("contract_id") != contract.get("contract_id")
                           or not _contract_matches_identity(state_contract)):
        reason = "stale_contract"
    elif not _evaluation_matches_candidate(evaluation, candidate):
        reason = "candidate_identity_mismatch"
    elif _valid_for_contract(evaluation, contract):
        if incumbent and (incumbent.get("protocol_id") != contract["protocol_id"]
                          or incumbent.get("contract_id") != contract["contract_id"]):
            reason = "incumbent_protocol_mismatch"
        elif incumbent and not any(
            e.get("evaluation_id") == incumbent.get("evaluation_id")
            and e.get("candidate_id") == incumbent.get("candidate_id")
            and _valid_for_contract(e, contract)
            and e.get("primary_value") == incumbent.get("primary_value")
            and any(_evaluation_matches_candidate(e, source) for source in candidates)
            for e in evaluations
        ):
            reason = "incumbent_evidence_mismatch"
        else:
            best = _number(incumbent.get("primary_value"))
            value = evaluation["primary_value"]
            improvement = value - best if best is not None else None
            if improvement is not None and pm["direction"] == "minimize":
                improvement *= -1
            accepted = best is None or improvement > pm.get("min_delta", 0)
            reason = "baseline" if best is None else "improved" if accepted else "no_improvement"
    if reason not in {
        "stale_contract", "candidate_identity_mismatch", "incumbent_protocol_mismatch",
    }:
        out["working_candidate_id"] = candidate["candidate_id"]
    if accepted:
        out["incumbent"] = {k: evaluation[k] for k in (
            "candidate_id", "evaluation_id", "primary_value", "protocol_id", "contract_id",
        )}
    promotion = {"evaluation_id": evaluation["evaluation_id"],
                 "candidate_id": candidate["candidate_id"],
                 "previous_candidate_id": incumbent.get("candidate_id"),
                 "accepted": accepted, "reason": reason,
                 "rule": {"direction": pm["direction"], "min_delta": pm.get("min_delta", 0)}}
    out["promotions"] = [*(out.get("promotions") or []), promotion]
    return out


def _criterion_truth(
    criterion: Any, evaluation: dict[str, Any], contract: dict[str, Any],
) -> bool | None:
    if not isinstance(criterion, dict):
        return None
    metric = criterion.get("metric")
    if not isinstance(metric, str) or not metric:
        return None
    if criterion.get("comparison") == "baseline_delta":
        summary = condition_summary(evaluation.get("metrics") or {}, contract)
        treatment = criterion.get("treatment")
        if summary is None or treatment not in summary["improvements_vs_baseline"]:
            return None
        if metric != contract["protocol"]["primary_metric"]["name"]:
            return None
        value = summary["improvements_vs_baseline"][treatment]
    else:
        value = _last((evaluation.get("metrics") or {}).get(metric))
    threshold = _number(criterion.get("threshold"))
    if value is None or threshold is None:
        return None
    operator = criterion.get("operator")
    if operator in {">", "gt"}:
        return value > threshold
    if operator in {">=", "ge", "gte"}:
        return value >= threshold
    if operator in {"<", "lt"}:
        return value < threshold
    if operator in {"<=", "le", "lte"}:
        return value <= threshold
    if operator in {"==", "eq"}:
        return value == threshold
    return None


def apply_scientific_verdicts(
    plan: dict[str, Any], updates: list[dict[str, Any]], *, contract: dict[str, Any],
    evaluations: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Treat LLM status as a proposal; publish only frozen observable criteria."""
    out = copy.deepcopy(plan)
    hypotheses = out.get("hypotheses") or []
    evidence = {}
    ambiguous_refs = set()
    for evaluation in evaluations:
        ref = evaluation.get("evaluation_id")
        if ref in evidence and evidence[ref] != evaluation:
            ambiguous_refs.add(ref)
        evidence[ref] = evaluation
    claims = {claim["index"]: claim for claim in contract.get("claims") or []}
    verdicts = []
    for update in updates:
        index = update.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index not in claims:
            continue
        if index >= len(hypotheses) or not isinstance(hypotheses[index], dict):
            continue
        claim = claims[index]
        refs = update.get("evidence_refs") or []
        refs = list(dict.fromkeys(refs)) if isinstance(refs, list) and all(
            isinstance(ref, str) for ref in refs) else []
        proposed = update.get("status", "testing")
        truth = []
        reason = ("missing_explicit_criterion" if not claim.get("criterion")
                  else "missing_evidence_refs")
        if not _contract_matches_identity(contract):
            reason = "contract_identity_mismatch"
        elif hypotheses[index].get("text") != claim.get("text") or (
            "criterion" in hypotheses[index]
            and hypotheses[index]["criterion"] != claim.get("criterion")
        ):
            reason = "claim_identity_mismatch"
        elif claim.get("criterion") and refs:
            if any(ref in ambiguous_refs or ref not in evidence
                   or not _valid_for_contract(evidence[ref], contract) for ref in refs):
                reason = "invalid_or_foreign_evidence"
            else:
                truth = [_criterion_truth(claim["criterion"], evidence[ref], contract)
                         for ref in refs]
                reason = "criterion_not_evaluable" if None in truth else "conflicting_evidence"
        verdict = "inconclusive"
        if truth and None not in truth and all(truth) and proposed == "verified":
            verdict, reason = "supported", "criterion_satisfied"
        elif truth and None not in truth and not any(truth) and proposed == "falsified":
            verdict, reason = "refuted", "criterion_not_satisfied"
        status = {"supported": "verified", "refuted": "falsified"}.get(verdict, "testing")
        hypothesis = hypotheses[index]
        hypothesis.update({"status": status, "proposed_status": proposed,
                           "evidence": str(update.get("evidence") or ""), "evidence_refs": refs,
                           "scientific_verdict": verdict, "verdict_reason": reason})
        verdicts.append({"claim_id": claim["claim_id"], "index": index,
                         "verdict": verdict, "proposed_status": proposed,
                         "criterion": copy.deepcopy(claim.get("criterion")),
                         "evidence_refs": refs, "reason": reason,
                         "scope": "observed under frozen protocol; "
                                  "causality and generalization unverified",
                         "contract_id": contract["contract_id"],
                         "protocol_id": contract["protocol_id"]})
    return out, verdicts


def _diagnostic_json(value: Any) -> Any:
    """Retain invalid numeric diagnostics without producing non-JSON artifacts."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_numeric_value": repr(value)}
    if isinstance(value, dict):
        return {str(key): _diagnostic_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_diagnostic_json(item) for item in value]
    return copy.deepcopy(value)


def _bundle_verdicts(
    verdicts: list[dict[str, Any]], valid: list[dict[str, Any]], contract: dict[str, Any],
) -> list[dict[str, Any]]:
    """A cached verdict cannot outlive the valid observations that support it."""
    claims = {claim["claim_id"]: claim for claim in contract.get("claims") or []}
    evidence = {evaluation["evaluation_id"]: evaluation for evaluation in valid}
    out = []
    for verdict in verdicts:
        record = copy.deepcopy(verdict)
        claim = claims.get(verdict.get("claim_id"))
        refs = verdict.get("evidence_refs") or []
        reason = None
        if (verdict.get("contract_id") != contract.get("contract_id")
                or verdict.get("protocol_id") != contract.get("protocol_id")
                or claim is None):
            reason = "stale_verdict_identity"
        elif verdict.get("verdict") in {"supported", "refuted"}:
            if (not isinstance(refs, list) or not refs or any(
                not isinstance(ref, str) or ref not in evidence for ref in refs
            )):
                reason = "invalid_or_foreign_evidence"
            else:
                truths = [_criterion_truth(claim.get("criterion"), evidence[ref], contract)
                          for ref in refs]
                expected = verdict["verdict"] == "supported"
                if not truths or any(truth is not expected for truth in truths):
                    reason = "criterion_not_evaluable_or_conflicting"
        if reason:
            record.update({"verdict": "inconclusive", "reason": reason})
        if claim is not None:
            record["criterion"] = copy.deepcopy(claim.get("criterion"))
        record["scope"] = ("observed under frozen protocol; "
                           "causality and generalization unverified")
        out.append(record)
    return out


def build_result_bundle(
    *, experiment_id: str, contract: dict[str, Any], candidates: list[dict[str, Any]],
    evaluations: list[dict[str, Any]], selected_candidate_id: str | None,
    verdicts: list[dict[str, Any]] | None = None, stopped_reason: str | None = None,
) -> dict[str, Any]:
    snapshots = {c["candidate_id"]: c for c in candidates if _candidate_matches_identity(c)}
    recorded = {}
    conflicting_replays = set()
    for evaluation in evaluations:
        evaluation_id = evaluation.get("evaluation_id")
        if evaluation_id in recorded and recorded[evaluation_id] != evaluation:
            conflicting_replays.add(evaluation_id)
        recorded[evaluation_id] = evaluation
    valid = []
    invalid = []
    for evaluation in recorded.values():
        candidate = snapshots.get(evaluation.get("candidate_id"))
        if (_valid_for_contract(evaluation, contract) and candidate
                and _evaluation_matches_candidate(evaluation, candidate)
                and evaluation.get("evaluation_id") not in conflicting_replays):
            valid.append(copy.deepcopy(evaluation))
        else:
            diagnostic = _diagnostic_json(evaluation)
            if not _valid_for_contract(evaluation, contract):
                diagnostic["invalid_reasons"] = list(dict.fromkeys([
                    *(diagnostic.get("invalid_reasons") or []), "evaluation_record_invalid",
                    *_metric_validation_reasons(
                        evaluation.get("metrics"), evaluation.get("primary_value"), contract,
                    ),
                ]))
            if not candidate or not _evaluation_matches_candidate(evaluation, candidate):
                diagnostic["invalid_reasons"] = [
                    *(diagnostic.get("invalid_reasons") or []), "candidate_source_unavailable",
                ]
            if evaluation.get("evaluation_id") in conflicting_replays:
                diagnostic["invalid_reasons"] = [
                    *(diagnostic.get("invalid_reasons") or []), "conflicting_evaluation_replay",
                ]
            diagnostic["valid"] = False
            invalid.append(diagnostic)
    selected = copy.deepcopy(snapshots.get(selected_candidate_id))
    selected_evaluations = [e for e in valid if e.get("candidate_id") == selected_candidate_id]
    if selected is None or not selected_evaluations:
        selected = None
        selected_evaluations = []
    reproduction = [{key: e.get(key) for key in (
        "evaluation_id", "run_id", "candidate_id", "protocol_id", "command", "log_path",
        "job_identity",
    )} for e in selected_evaluations]
    log_references = [{"evaluation_id": e.get("evaluation_id"), "log_path": e.get("log_path"),
                       "job_identity": copy.deepcopy(e.get("job_identity"))}
                      for e in [*valid, *invalid] if e.get("log_path") or e.get("job_identity")]
    scientific_verdicts = _bundle_verdicts(verdicts or [], valid, contract)
    claims = contract.get("claims") or []
    resolved_claims = {verdict.get("claim_id") for verdict in scientific_verdicts
                       if verdict.get("verdict") in {"supported", "refuted"}}
    scientific_outcome = ("not_requested" if not claims else "observational_criteria_resolved"
                          if all(claim["claim_id"] in resolved_claims for claim in claims)
                          else "inconclusive")
    bundle = {"schema_version": SCHEMA_VERSION, "experiment_id": str(experiment_id),
              "contract_id": contract["contract_id"], "protocol_id": contract["protocol_id"],
              "research_contract": copy.deepcopy(contract), "selected_candidate": selected,
              "selected_evaluations": selected_evaluations, "valid_evaluations": valid,
              "invalid_evaluations": invalid, "candidate_history": _diagnostic_json(candidates),
              "reproduction": reproduction, "log_references": log_references,
              "failed_diagnostics": [{key: e.get(key) for key in (
                  "evaluation_id", "candidate_id", "status", "exit_code", "invalid_reasons",
                  "metrics", "command", "log_path", "job_identity",
              )} for e in invalid],
              "scientific_verdicts": scientific_verdicts, "scientific_outcome": scientific_outcome,
              "stopped_reason": stopped_reason,
              "execution_outcome": "observations_available" if valid else "no_valid_evaluation",
              "optimization_outcome": "selected" if selected else "no_selected_candidate"}
    bundle["bundle_id"] = "bundle:" + _digest(bundle)
    return bundle
