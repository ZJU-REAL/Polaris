"""Issue #511: persisted, permission-safe incremental discovery schedules."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.literature_discovery import (
    LiteratureDiscoverySchedule,
    LiteratureSearchHit,
    LiteratureSearchRun,
)
from app.models.paper import new_paper
from app.schemas.literature_discovery import LiteratureDiscoveryScheduleUpdate
from app.services.literature.discovery_schedules import (
    claim_due_schedules,
    next_occurrence,
    record_dispatch_result,
    upsert_schedule,
)
from app.services.literature.incremental_filter import filter_known_candidates
from tests.conftest import register_and_login
from worker.tasks import dispatch_literature_discovery_schedules


async def _library(client) -> tuple[DirectionLibrary, object]:
    token = await register_and_login(client, email="schedule-service-owner@example.com")
    response = await client.post(
        "/api/libraries",
        json={"name": "Scheduled mechanics", "statement": "Dynamic structural response"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 201, response.text
    library_id = uuid.UUID(response.json()["id"])
    async with get_sessionmaker()() as session:
        library = await session.get(DirectionLibrary, library_id)
        assert library is not None
        actor_id = library.submitted_by
        session.expunge(library)
    return library, actor_id


def test_next_occurrence_respects_library_timezone():
    after = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    assert next_occurrence(
        timezone="Asia/Shanghai", hour=9, minute=30, after=after
    ) == datetime(2026, 8, 28, 1, 30, tzinfo=UTC)


@pytest.mark.asyncio
async def test_due_schedule_creates_one_versioned_run_and_recovers_undispatched_queue(client):
    library, actor_id = await _library(client)
    now = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
    async with get_sessionmaker()() as session:
        attached = await session.get(DirectionLibrary, library.id)
        schedule = await upsert_schedule(
            session,
            library=attached,
            data=LiteratureDiscoveryScheduleUpdate(
                enabled=True,
                timezone="UTC",
                hour=1,
                minute=30,
                requested_count=50,
                candidate_budget=200,
                start_year=2016,
            ),
            actor_id=actor_id,
            now=now - timedelta(days=1),
        )
        schedule.next_run_at = now - timedelta(minutes=1)
        await session.commit()

        first = await claim_due_schedules(session, now=now)
        second = await claim_due_schedules(session, now=now + timedelta(minutes=1))
        run_count = await session.scalar(
            select(func.count()).select_from(LiteratureSearchRun)
        )
        run = await session.get(LiteratureSearchRun, first[0])
        persisted = await session.get(LiteratureDiscoverySchedule, library.id)

        assert first == second
        assert run_count == 1
        assert run is not None
        assert run.trigger == "scheduled"
        assert run.schedule_version == 1
        assert run.requested_count == 50
        assert run.start_year == 2016
        assert run.source_config["incremental_discovery"]["schedule_version"] == 1
        assert persisted.next_run_at > now

        await record_dispatch_result(session, run_id=run.id, ok=True, now=now)
        assert await claim_due_schedules(session, now=now + timedelta(minutes=2)) == []


@pytest.mark.asyncio
async def test_incremental_filter_removes_historical_hits_and_library_papers(client):
    library, actor_id = await _library(client)
    async with get_sessionmaker()() as session:
        prior = LiteratureSearchRun(
            library_id=library.id,
            created_by=actor_id,
            requested_count=10,
            candidate_budget=20,
            topic="prior",
            trigger="scheduled",
        )
        session.add(prior)
        await session.flush()
        session.add(
            LiteratureSearchHit(
                run_id=prior.id,
                source="crossref",
                dedup_key="doi:10.1000/history",
                title="Historical result",
            )
        )
        paper = new_paper(
            source="arxiv",
            dedup_key="arxiv:2608.12345",
            arxiv_id="2608.12345",
            doi=None,
            external_ids=None,
            title="Already in library",
            authors=[{"name": "Ada Example"}],
            abstract=None,
            year=2026,
            venue=None,
            url=None,
        )
        session.add(paper)
        await session.flush()
        session.add(
            LibraryPaper(
                library_id=library.id,
                paper_id=paper.id,
                status="scored",
            )
        )
        await session.commit()

        unseen, removed = await filter_known_candidates(
            session,
            library_id=library.id,
            candidates=[
                {"title": "Historical result", "doi": "10.1000/HISTORY"},
                {"title": "Already in library", "arxiv_id": "2608.12345"},
                {"title": "New evidence", "doi": "10.1000/new"},
            ],
        )

    assert removed == 2
    assert [item["title"] for item in unseen] == ["New evidence"]


@pytest.mark.asyncio
async def test_incremental_filter_batches_the_maximum_candidate_pool(client):
    library, _ = await _library(client)
    candidates = [
        {"title": f"Candidate {index}", "doi": f"10.2000/{index}"}
        for index in range(1000)
    ]
    async with get_sessionmaker()() as session:
        unseen, removed = await filter_known_candidates(
            session,
            library_id=library.id,
            candidates=candidates,
        )

    assert removed == 0
    assert len(unseen) == 1000


@pytest.mark.asyncio
async def test_worker_dispatches_due_schedule_and_records_enqueue(client):
    library, actor_id = await _library(client)
    async with get_sessionmaker()() as session:
        attached = await session.get(DirectionLibrary, library.id)
        schedule = await upsert_schedule(
            session,
            library=attached,
            data=LiteratureDiscoveryScheduleUpdate(
                enabled=True,
                timezone="UTC",
                hour=1,
                minute=30,
            ),
            actor_id=actor_id,
        )
        schedule.next_run_at = datetime.now(UTC) - timedelta(minutes=1)
        await session.commit()

    calls = []

    class Redis:
        async def enqueue_job(self, name, run_id, **kwargs):
            calls.append((name, run_id, kwargs))

    dispatched = await dispatch_literature_discovery_schedules({"redis": Redis()})

    assert len(dispatched) == 1
    assert calls[0][0] == "run_literature_discovery"
    assert calls[0][1] == dispatched[0]
    assert calls[0][2]["_job_id"].startswith("scheduled-literature-")
    async with get_sessionmaker()() as session:
        schedule = await session.get(LiteratureDiscoverySchedule, library.id)
        assert schedule is not None
        assert str(schedule.last_run_id) == dispatched[0]
        assert schedule.last_enqueued_at is not None
        assert schedule.last_error_code is None
