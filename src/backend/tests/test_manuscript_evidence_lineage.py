"""ResultBundle lineage and stale-review regressions; no model or remote execution."""

import asyncio
import copy
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from app.agents.voyage import actions_review
from app.agents.voyage.actions import ActionContext
from app.agents.voyage.actions_review import review_guardrail
from app.core.db import get_sessionmaker
from app.core.voyage_lease import VoyageLeaseLost, claim_execution
from app.models.activity import Activity
from app.models.base import utcnow
from app.models.experiment import Experiment, ExperimentRun
from app.models.gate import Gate
from app.models.idea import Idea
from app.models.manuscript import Manuscript, ManuscriptFile
from app.models.review import ReviewSession
from app.models.voyage import VoyageRun
from app.services import experiment_evidence as evidence
from app.services import manuscripts as ms_service
from app.services import paper_review
from tests.test_manuscripts import (
    _create_manuscript,
    _mark_review_passed,
    _seed_experiment,
    _seed_idea,
    _setup_project,
)


def _versioned_experiment():
    experiment = Experiment(id=uuid.uuid4(), project_id=uuid.uuid4(), plan={})
    plan = {
        "primary_metric": {"name": "accuracy", "direction": "maximize"},
        "hypotheses": [
            {
                "text": "Frozen claim",
                "criterion": {"metric": "accuracy", "operator": ">=", "threshold": 0.8},
            }
        ],
    }
    contract = evidence.freeze_research_contract(plan)
    best = evidence.snapshot_candidate({"train.py": "best"})
    worse = evidence.snapshot_candidate({"train.py": "worse"}, parent_id=best["candidate_id"])
    evaluations = []
    runs = []
    for seq, candidate, value, status, code in (
        (1, best, 0.9, "succeeded", 0),
        (2, worse, 0.7, "succeeded", 0),
        (3, worse, 99.0, "failed", 1),
    ):
        run_id = uuid.uuid4()
        metrics = {"accuracy": [{"step": 1, "value": value}]}
        runs.append(
            ExperimentRun(
                id=run_id,
                seq=seq,
                command="run",
                status=status,
                exit_code=code,
                metrics=metrics,
                primary_value=value,
            )
        )
        evaluations.append(
            evidence.make_evaluation(
                contract=contract,
                candidate=candidate,
                run_id=str(run_id),
                seq=seq,
                status=status,
                exit_code=code,
                metrics=metrics,
                primary_value=value,
            )
        )
    _, verdicts = evidence.apply_scientific_verdicts(
        plan,
        [
            {
                "index": 0,
                "status": "verified",
                "evidence_refs": [evaluations[0]["evaluation_id"]],
            }
        ],
        contract=contract,
        evaluations=evaluations,
    )
    bundle = evidence.build_result_bundle(
        experiment_id=str(experiment.id),
        contract=contract,
        candidates=[best, worse],
        evaluations=evaluations,
        selected_candidate_id=best["candidate_id"],
        verdicts=verdicts,
    )
    experiment.runs = runs
    experiment.iteration_state = {
        "result_bundle": bundle,
        "research_contract": contract,
        "code_files": best["files"],
    }
    experiment.plan = plan | {"hypotheses": [{"text": "Later mutable claim", "status": "verified"}]}
    experiment.figures = [
        {"index": 0, "caption": "current", "bundle_id": bundle["bundle_id"]},
        {"index": 1, "caption": "stale", "bundle_id": "bundle:old"},
    ]
    return experiment, bundle


def test_versioned_facts_use_valid_evaluations_and_selected_candidate():
    experiment, bundle = _versioned_experiment()
    metrics = ms_service._metrics_pack(experiment)
    assert metrics[0]["best"] == 0.9
    assert [r["value"] for r in metrics[0]["runs"]] == [0.9, 0.7]
    assert [r["selected"] for r in metrics[0]["runs"]] == [True, False]
    assert metrics[0]["provenance"]["bundle_id"] == bundle["bundle_id"]
    metadata = ms_service._bundle_metadata(experiment)
    assert metadata["selected_candidate_id"] == bundle["selected_candidate"]["candidate_id"]
    assert metadata["selected_evaluation_ids"] == [
        bundle["selected_evaluations"][0]["evaluation_id"]
    ]
    assert len(metadata["invalid_evaluation_ids"]) == 1
    # Source contents do not need to be duplicated into a manuscript fact pack.
    assert "files" not in metadata
    figures = ms_service._figures_pack(experiment)
    assert [f["caption"] for f in figures] == ["current"]
    assert figures[0]["bundle_id"] == bundle["bundle_id"]
    assert len(figures[0]["evidence_refs"]) == 2
    hypothesis = ms_service._hypotheses_pack(experiment)[0]
    assert hypothesis["text"] == "Frozen claim"
    assert hypothesis["status"] == "verified"
    assert hypothesis["evidence_refs"] == [bundle["selected_evaluations"][0]["evaluation_id"]]


def test_invalid_or_foreign_metrics_and_scientific_refs_do_not_enter_facts():
    experiment, bundle = _versioned_experiment()
    modified = copy.deepcopy(bundle)
    bad = copy.deepcopy(bundle["valid_evaluations"][0])
    bad.update(seq=4, status="failed", metrics={"accuracy": [{"value": 1000.0}]})
    modified["valid_evaluations"].append(bad)
    bad = copy.deepcopy(bad)
    bad.update(seq=5, status="succeeded", protocol_id="protocol:foreign")
    modified["valid_evaluations"].append(bad)
    modified["scientific_verdicts"][0]["evidence_refs"] = ["run:missing"]
    experiment.iteration_state = {"result_bundle": modified}
    assert [r["value"] for r in ms_service._metrics_pack(experiment)[0]["runs"]] == [0.9, 0.7]
    assert ms_service._hypotheses_pack(experiment)[0]["status"] == "testing"
    experiment.iteration_state = {"result_bundle": {"experiment_id": "foreign"}}
    assert ms_service._metrics_pack(experiment) == []
    assert ms_service._figures_pack(experiment) == []


def test_legacy_observations_are_finite_successful_and_scientifically_unverified():
    experiment, _ = _versioned_experiment()
    experiment.iteration_state = {}
    experiment.runs += [
        ExperimentRun(
            id=uuid.uuid4(),
            seq=4,
            command="run",
            status="succeeded",
            exit_code=0,
            metrics={"accuracy": [{"value": float("inf")}]},
        )
    ]
    experiment.runs += [
        ExperimentRun(
            id=uuid.uuid4(),
            seq=5,
            command="run",
            status="succeeded",
            exit_code=0,
            metrics={"accuracy": [{"value": True}]},
        )
    ]
    assert [r["value"] for r in ms_service._metrics_pack(experiment)[0]["runs"]] == [0.9, 0.7]
    assert ms_service._metrics_pack(experiment)[0]["provenance"]["kind"] == "legacy_unversioned"
    assert ms_service._hypotheses_pack(experiment)[0]["status"] == "testing"
    assert ms_service._hypotheses_pack(experiment)[0]["scientific_verdict"] == "inconclusive"


async def _ready_manuscript(client, *, experiment=False):
    project_id, headers = await _setup_project(client)
    kwargs = {}
    if experiment:
        idea_id = await _seed_idea(project_id)
        kwargs = {"idea_id": idea_id, "experiment_id": await _seed_experiment(project_id, idea_id)}
    response = await _create_manuscript(client, headers, project_id, **kwargs)
    manuscript_id = uuid.UUID(response.json()["id"])
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        manuscript.latest_compile = {"version": 1, "status": "ok", "pdf_available": True}
        manuscript.status = "compiled"
        await _mark_review_passed(session, manuscript)
        await session.commit()
    return manuscript_id, headers


@pytest.mark.parametrize("field", ["content", "path", "title", "engine"])
async def test_all_persisted_source_edit_paths_invalidate_review(client, field):
    manuscript_id, headers = await _ready_manuscript(client)
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        if field in {"title", "engine"}:
            setattr(manuscript, field, "changed")
        else:
            file = (
                await session.execute(
                    select(ManuscriptFile).where(
                        ManuscriptFile.manuscript_id == manuscript_id,
                        ManuscriptFile.path == "main.tex",
                    )
                )
            ).scalar_one()
            setattr(file, field, "changed")
        await session.commit()
    response = await client.post(f"/api/manuscripts/{manuscript_id}/submit", headers=headers)
    assert response.status_code == 409 and response.json()["detail"] == "REVIEW_REQUIRED"
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        assert manuscript.review_passed is False


async def test_evidence_refresh_preserves_same_identity_and_invalidates_changed_identity(client):
    manuscript_id, _ = await _ready_manuscript(client, experiment=True)
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        old_binding = manuscript.fact_pack["review_binding"]
        await ms_service.refresh_fact_pack(session, manuscript)
        assert manuscript.review_passed is True
        assert manuscript.fact_pack["review_binding"] == old_binding
        experiment = await session.get(Experiment, manuscript.experiment_id)
        experiment.plan = dict(experiment.plan) | {
            "primary_metric": {"name": "accuracy", "direction": "minimize"}
        }
        await session.commit()
        await ms_service.refresh_fact_pack(session, manuscript)
        assert manuscript.review_passed is False
        assert "review_binding" not in manuscript.fact_pack


async def test_submit_rechecks_evidence_changes_without_manual_refresh(client):
    manuscript_id, headers = await _ready_manuscript(client, experiment=True)
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        experiment = await session.get(Experiment, manuscript.experiment_id)
        experiment.iteration_state = dict(experiment.iteration_state or {}) | {
            "code_files": {"train.py": "new candidate"}
        }
        await session.commit()
    response = await client.post(f"/api/manuscripts/{manuscript_id}/submit", headers=headers)
    assert response.status_code == 409 and response.json()["detail"] == "REVIEW_REQUIRED"


async def test_guardrail_rejects_stale_review_and_rechecks_finalized_checkpoint(client):
    manuscript_id, _ = await _ready_manuscript(client)
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        run = VoyageRun(id=uuid.uuid4(), project_id=manuscript.project_id, kind="paper_review")
        checkpoint = {
            "params": {"manuscript_id": str(manuscript_id)},
            "review_binding": manuscript.fact_pack["review_binding"],
            "reviews": [{"persona": "reviewer", "weaknesses": [], "unreliable": False}],
            "meta": {"rating": 9.0},
            "citation_check": {"items": []},
        }
        manuscript.title = "Changed during review"
        await session.commit()
    ctx = ActionContext(run=run, llm=None, checkpoint=checkpoint)
    result = await review_guardrail(ctx, {})
    assert result["passed"] is False
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        review = await session.get(ReviewSession, uuid.UUID(ctx.checkpoint["review_session_id"]))
        assert manuscript.review_passed is False
        assert review.payload["binding_current"] is False
        assert review.payload["review_binding"] == checkpoint["review_binding"]
    # Cached completion cannot authorize a new source version.
    ctx.checkpoint["review_result"] = True
    result = await review_guardrail(ctx, {})
    assert result == {"passed": False, "skipped": True}


async def test_gate_approval_rechecks_source_binding_and_keeps_pending(
    client, queue_stub, bus_recorder
):
    manuscript_id, headers = await _ready_manuscript(client, experiment=True)
    response = await client.post(f"/api/manuscripts/{manuscript_id}/submit", headers=headers)
    assert response.status_code == 201, response.text
    gate_id = response.json()["id"]
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        experiment = await session.get(Experiment, manuscript.experiment_id)
        experiment.iteration_state = dict(experiment.iteration_state or {}) | {
            "code_files": {"train.py": "changed after submit"}
        }
        await session.commit()
    response = await client.post(f"/api/gates/{gate_id}/approve", json={}, headers=headers)
    assert response.status_code == 409 and response.json()["detail"] == "REVIEW_REQUIRED"
    async with get_sessionmaker()() as session:
        gate = await session.get(Gate, uuid.UUID(gate_id))
        assert gate.status == "pending"
    # The explicit override remains a conscious approval path after freshness failure.
    response = await client.post(
        f"/api/gates/{gate_id}/approve", json={"override": True}, headers=headers
    )
    assert response.status_code == 200, response.text


async def test_foreign_project_experiment_cannot_supply_manuscript_facts(client):
    manuscript_id, headers = await _ready_manuscript(client)
    project_id = (
        await client.post("/api/projects", json={"name": "other-project"}, headers=headers)
    ).json()["id"]
    idea_id = await _seed_idea(project_id)
    experiment_id = await _seed_experiment(project_id, idea_id)
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        manuscript.experiment_id = uuid.UUID(experiment_id)
        pack = await ms_service.build_fact_pack(session, manuscript)
        assert pack["metrics"] == [] and pack["hypotheses"] == [] and pack["figures"] == []
        assert pack["evidence_bundle"] is None


async def test_review_requires_compile_for_current_source(client):
    manuscript_id, _ = await _ready_manuscript(client)
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        manuscript.latest_compile = dict(manuscript.latest_compile) | {"source_digest": "old"}
        await session.commit()
        with pytest.raises(ms_service.CompileRequiredError):
            await paper_review.create_review_voyage(
                session, manuscript=manuscript, personas=None, created_by=uuid.uuid4()
            )


async def test_binary_asset_mutation_and_compile_version_invalidate_bound_review(client):
    manuscript_id, headers = await _ready_manuscript(client)
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        # A valid image asset is part of the reviewed source identity.
        session.add(
            ManuscriptFile(
                manuscript_id=manuscript_id, path="diagram.png", content="", is_binary=True
            )
        )
        ms_service.write_binary_asset(manuscript_id, "diagram.png", b"original")
        await session.commit()
        await _mark_review_passed(session, manuscript)
        await session.commit()
    ms_service.write_binary_asset(manuscript_id, "diagram.png", b"changed")
    response = await client.post(f"/api/manuscripts/{manuscript_id}/submit", headers=headers)
    assert response.status_code == 409 and response.json()["detail"] == "REVIEW_REQUIRED"
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        await _mark_review_passed(session, manuscript)
        await session.commit()
        manuscript.latest_compile = dict(manuscript.latest_compile) | {"version": 2}
        await session.commit()
    response = await client.post(f"/api/manuscripts/{manuscript_id}/submit", headers=headers)
    assert response.status_code == 409 and response.json()["detail"] == "REVIEW_REQUIRED"


async def test_legacy_boolean_pass_without_binding_cannot_authorize_submission(client):
    manuscript_id, headers = await _ready_manuscript(client)
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        pack = dict(manuscript.fact_pack)
        pack.pop("review_binding")
        manuscript.fact_pack = pack
        manuscript.review_passed = True
        await session.commit()
    response = await client.post(f"/api/manuscripts/{manuscript_id}/submit", headers=headers)
    assert response.status_code == 409 and response.json()["detail"] == "REVIEW_REQUIRED"


@pytest.mark.parametrize("termination", ["takeover", "cancelled"])
async def test_superseded_review_guardrail_cannot_overwrite_successors_failed_verdict(
    client, monkeypatch, termination
):
    manuscript_id, _ = await _ready_manuscript(client)
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        run = VoyageRun(
            kind="paper_review",
            goal="review source",
            project_id=manuscript.project_id,
            status="executing",
        )
        review = ReviewSession(
            target_type="manuscript",
            target_id=manuscript_id,
            payload={"passed": False, "owner": "initial"},
        )
        session.add_all([run, review])
        await session.commit()
        checkpoint = {
            "params": {"manuscript_id": str(manuscript_id)},
            "review_session_id": str(review.id),
            "review_binding": manuscript.fact_pack["review_binding"],
            "reviews": [{"persona": "old reviewer", "weaknesses": [], "unreliable": False}],
            "meta": {"rating": 9.0},
            "citation_check": {"items": []},
        }
        run_id, review_id = run.id, review.id
    checked, release = asyncio.Event(), asyncio.Event()
    original_check = ms_service.review_binding_matches

    async def blocked_check(session, manuscript, binding):
        current = await original_check(session, manuscript, binding)
        checked.set()
        await release.wait()
        return current

    monkeypatch.setattr(ms_service, "review_binding_matches", blocked_check)

    async def old_owner():
        async with claim_execution(run_id) as acquired:
            assert acquired
            ctx = ActionContext(run=run, llm=None, checkpoint=checkpoint)
            await actions_review.review_guardrail(ctx, {})

    task = asyncio.create_task(old_owner())
    successor = uuid.uuid4()
    try:
        await asyncio.wait_for(checked.wait(), 2)
        async with get_sessionmaker()() as session:
            if termination == "takeover":
                await session.execute(
                    update(VoyageRun)
                    .where(VoyageRun.id == run_id)
                    .values(
                        execution_token=successor,
                        execution_expires_at=utcnow() + timedelta(minutes=1),
                    )
                )
            else:
                await session.execute(
                    update(VoyageRun).where(VoyageRun.id == run_id).values(status="cancelled")
                )
            manuscript = await session.get(Manuscript, manuscript_id)
            manuscript.review_passed = False
            review = await session.get(ReviewSession, review_id)
            review.payload = {"passed": False, "owner": "successor"}
            await session.commit()
        release.set()
        with pytest.raises(VoyageLeaseLost):
            await asyncio.wait_for(task, 2)
        async with get_sessionmaker()() as session:
            manuscript = await session.get(Manuscript, manuscript_id)
            review = await session.get(ReviewSession, review_id)
            assert manuscript.review_passed is False
            assert review.payload == {"passed": False, "owner": "successor"}
            stored_run = await session.get(VoyageRun, run_id)
            assert stored_run.execution_token == (successor if termination == "takeover" else None)
            assert not (
                await session.scalars(
                    select(Activity).where(Activity.kind == "manuscript.review_completed")
                )
            ).all()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_versioned_run_without_published_bundle_cannot_fall_back_to_raw_legacy_facts():
    experiment, _ = _versioned_experiment()
    state = dict(experiment.iteration_state)
    state.pop("result_bundle")
    experiment.iteration_state = state
    assert ms_service._metrics_pack(experiment) == []
    assert ms_service._hypotheses_pack(experiment) == []
    assert ms_service._figures_pack(experiment) == []
    assert ms_service._bundle_metadata(experiment)["provenance"] == "versioned_result_bundle"


async def test_gate_captured_review_a_cannot_approve_current_review_b_without_override(
    client, queue_stub, bus_recorder,
):
    manuscript_id, headers = await _ready_manuscript(client)
    response = await client.post(f"/api/manuscripts/{manuscript_id}/submit", headers=headers)
    assert response.status_code == 201, response.text
    gate_id = uuid.UUID(response.json()["id"])
    captured_binding = copy.deepcopy(response.json()["payload"]["review_binding"])

    # B is genuinely a different source version and has a new passing review.
    # A boolean review_passed check alone would accidentally authorize A's gate.
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        main = (await session.scalars(select(ManuscriptFile).where(
            ManuscriptFile.manuscript_id == manuscript_id, ManuscriptFile.path == "main.tex",
        ))).one()
        main.content += "\n% Revised submission B\n"
        manuscript.title = "Revised submission B"
        manuscript.latest_compile = dict(manuscript.latest_compile) | {"version": 2}
        await session.commit()
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        await _mark_review_passed(session, manuscript)
        await session.commit()
        current_binding = copy.deepcopy(manuscript.fact_pack["review_binding"])
        assert current_binding != captured_binding
        assert await ms_service.review_is_current(session, manuscript) is True

    notifications_before = len(bus_recorder.notify)
    response = await client.post(f"/api/gates/{gate_id}/approve", json={}, headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"] == "SUBMISSION_VERSION_CHANGED"
    assert len(bus_recorder.notify) == notifications_before
    async with get_sessionmaker()() as session:
        gate = await session.get(Gate, gate_id)
        manuscript = await session.get(Manuscript, manuscript_id)
        assert gate.status == "pending"
        assert gate.payload["review_binding"] == captured_binding
        assert manuscript.review_passed is True
        assert manuscript.fact_pack["review_binding"] == current_binding
        assert manuscript.status == "under_review"

    response = await client.post(
        f"/api/gates/{gate_id}/approve",
        json={"override": True, "comment": "Approve revised version B explicitly"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "approved"
    assert response.json()["comment"] == "Approve revised version B explicitly"
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        assert manuscript.status == "submitted"
        assert manuscript.fact_pack["review_binding"] == current_binding


async def test_fact_pack_keeps_frozen_idea_metadata_after_live_idea_is_renamed(client):
    project_id, headers = await _setup_project(client)
    idea_id = await _seed_idea(project_id)
    experiment_id = await _seed_experiment(project_id, idea_id)
    async with get_sessionmaker()() as session:
        idea = await session.get(Idea, uuid.UUID(idea_id))
        experiment = await session.get(Experiment, uuid.UUID(experiment_id))
        frozen_details = {"idea_id": idea_id, "title": idea.title, "summary": idea.summary,
                          "goal": "Answer the original retrieval research question"}
        contract = evidence.freeze_research_contract(
            experiment.plan, objective=frozen_details["goal"], objective_details=frozen_details,
        )
        candidate = evidence.snapshot_candidate({"train.py": "# frozen selected source\n"})
        runs = (await session.scalars(select(ExperimentRun).where(
            ExperimentRun.experiment_id == experiment.id,
        ).order_by(ExperimentRun.seq))).all()
        evaluations = [evidence.make_evaluation(
            contract=contract, candidate=candidate, run_id=str(run.id), seq=run.seq,
            status=run.status, exit_code=run.exit_code, metrics=run.metrics,
            primary_value=run.primary_value,
        ) for run in runs]
        bundle = evidence.build_result_bundle(
            experiment_id=experiment_id, contract=contract, candidates=[candidate],
            evaluations=evaluations, selected_candidate_id=candidate["candidate_id"],
        )
        experiment.iteration_state = {"research_contract": contract, "candidates": [candidate],
                                      "evaluations": evaluations, "result_bundle": bundle}
        await session.commit()
    response = await _create_manuscript(
        client, headers, project_id, idea_id=idea_id, experiment_id=experiment_id,
    )
    assert response.status_code == 201, response.text
    manuscript_id = uuid.UUID(response.json()["id"])
    async with get_sessionmaker()() as session:
        manuscript = await session.get(Manuscript, manuscript_id)
        assert manuscript.fact_pack["idea"] == {
            "title": frozen_details["title"], "summary": frozen_details["summary"],
            "goal": frozen_details["goal"], "provenance": "research_contract",
        }
        manuscript.latest_compile = {"version": 1, "status": "ok", "pdf_available": True}
        manuscript.status = "compiled"
        await _mark_review_passed(session, manuscript)
        await session.commit()
        original_pack = copy.deepcopy(manuscript.fact_pack)
        original_digest = ms_service._fact_pack_digest(original_pack)
        original_binding = copy.deepcopy(original_pack["review_binding"])
    async with get_sessionmaker()() as session:
        idea = await session.get(Idea, uuid.UUID(idea_id))
        idea.title = "A different idea title after the contract froze"
        idea.summary = "A changed summary must not silently replace the research question"
        await session.commit()

    response = await client.post(
        f"/api/manuscripts/{manuscript_id}/fact-pack/refresh", headers=headers,
    )
    assert response.status_code == 200, response.text
    refreshed = response.json()
    assert refreshed["idea"] == original_pack["idea"]
    assert ms_service._fact_pack_digest(refreshed) == original_digest
    assert refreshed["review_binding"] == original_binding
    async with get_sessionmaker()() as session:
        idea = await session.get(Idea, uuid.UUID(idea_id))
        assert idea.title != refreshed["idea"]["title"]
        manuscript = await session.get(Manuscript, manuscript_id)
        assert manuscript.review_passed is True
        assert await ms_service.review_is_current(session, manuscript) is True
