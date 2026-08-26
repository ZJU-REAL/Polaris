"""Issue #473: execute source adapters and persist ranked candidates."""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.models.literature_discovery import (
    LiteratureSearchHit,
    LiteratureSourceAttempt,
)
from app.models.paper import Paper
from app.schemas.literature_discovery import LiteratureCandidate, SourceSearchPage
from app.services.literature.runtime import AdapterRegistry, run_discovery
from tests.conftest import register_and_login


class FakeAdapter:
    def __init__(
        self,
        name: str,
        items: list[LiteratureCandidate],
        *,
        error: Exception | None = None,
    ):
        self.name = name
        self.items = items
        self.error = error
        self.requests = []

    async def search(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return SourceSearchPage(source=self.name, items=self.items, fetched_count=len(self.items))


def _candidate(source: str, title: str, *, doi: str | None = None, year: int = 2024):
    return LiteratureCandidate(
        source=source,
        title=title,
        abstract=f"Abstract for {title}",
        authors=[{"name": "A. Author"}],
        year=year,
        venue="Journal of Tests",
        doi=doi,
        url=f"https://example.test/{source}",
    )


async def _create_run(client, *, source_config, requested_count=2, candidate_budget=5):
    token = await register_and_login(client, email=f"runtime-{uuid.uuid4().hex}@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    library = await client.post(
        "/api/libraries",
        json={"name": "Runtime library", "statement": "Runtime test"},
        headers=headers,
    )
    assert library.status_code == 201, library.text
    response = await client.post(
        f"/api/libraries/{library.json()['id']}/literature/runs",
        json={
            "requested_count": requested_count,
            "candidate_budget": candidate_budget,
            "start_year": 2016,
            "end_year": 2025,
            "topic": "structural impact response",
            "source_config": source_config,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"]), headers, response.json()["library_id"]


@pytest.mark.asyncio
async def test_runtime_deduplicates_isolates_failures_and_persists_progress(client):
    run_id, _, _ = await _create_run(
        client,
        source_config={"sources": ["openalex", "semantic", "arxiv", "crossref", "core"]},
        requested_count=2,
        candidate_budget=5,
    )
    duplicate_doi = "10.1234/shared"
    openalex = FakeAdapter(
        "openalex",
        [
            _candidate("openalex", "Shared impact study", doi=duplicate_doi),
            _candidate("openalex", "Unique study", year=2023),
        ],
    )
    semantic = FakeAdapter(
        "semantic",
        [_candidate("semantic", "Shared impact study", doi=duplicate_doi, year=2022)],
    )
    arxiv = FakeAdapter("arxiv", [_candidate("arxiv", "Exploratory arXiv study", year=2025)])
    core = FakeAdapter("core", [], error=RuntimeError("core unavailable"))

    async with get_sessionmaker()() as session:
        run = await run_discovery(
            session,
            run_id,
                registry=AdapterRegistry((openalex, semantic, arxiv, core)),
            now=datetime(2026, 8, 26, tzinfo=UTC),
        )
        assert run.status == "partial"
        assert run.progress == {
            "phase": "completed",
            "source": "core",
            "fetched": 4,
            "accepted": 2,
            "requested_count": 2,
            "candidate_budget": 5,
            "returned_count": 2,
        }
        attempts = list(
            (
                await session.execute(
                    select(LiteratureSourceAttempt).where(LiteratureSourceAttempt.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        assert {item.source: item.status for item in attempts} == {
            "openalex": "completed",
            "semantic": "completed",
            "arxiv": "completed",
            "crossref": "skipped",
            "core": "partial",
        }
        hits = list(
            (
                await session.execute(
                    select(LiteratureSearchHit).where(LiteratureSearchHit.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(hits) == 2
        shared = next(hit for hit in hits if hit.doi == duplicate_doi)
        assert shared.metadata_snapshot["sources"] == ["openalex", "semantic"]
        assert await session.scalar(select(func.count()).select_from(Paper)) == 0

    assert [request.start_year for request in openalex.requests] == [2016]
    assert [request.end_year for request in arxiv.requests] == [2025]
    assert [request.limit for request in semantic.requests] == [5]


@pytest.mark.asyncio
async def test_runtime_marks_missing_sources_as_failed(client):
    run_id, _, _ = await _create_run(client, source_config={})
    async with get_sessionmaker()() as session:
        run = await run_discovery(session, run_id, registry=AdapterRegistry())
        assert run.status == "failed"
        assert run.error_summary == "NO_SOURCES_CONFIGURED"
        assert run.progress["requested_count"] == run.requested_count
        assert run.progress["candidate_budget"] == run.candidate_budget
        assert run.progress["returned_count"] == 0


@pytest.mark.asyncio
async def test_start_endpoint_enqueues_without_overwriting_requested_count(client, queue_stub):
    run_id, headers, library_id = await _create_run(
        client,
        source_config={"sources": ["openalex"]},
        requested_count=7,
        candidate_budget=80,
    )
    response = await client.post(
        f"/api/libraries/{library_id}/literature/runs/{run_id}/start",
        headers=headers,
    )
    assert response.status_code == 202, response.text
    assert response.json()["requested_count"] == 7
    assert queue_stub.jobs == [("run_literature_discovery", (str(run_id),), {})]
