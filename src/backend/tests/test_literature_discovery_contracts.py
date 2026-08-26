"""检索领域合同的确定性和持久化回归。"""

import uuid

import pytest
from sqlalchemy import delete, select

from app.core.db import get_sessionmaker
from app.models.library_direction import DirectionLibrary
from app.models.literature_discovery import (
    LiteratureSearchHit,
    LiteratureSearchRun,
    LiteratureSourceAttempt,
)
from app.schemas.literature_discovery import LiteratureCandidate, LiteratureSearchRequest
from app.services.literature.discovery import candidate_dedup_key, validate_candidate


def test_candidate_identity_prefers_stable_external_ids() -> None:
    doi = LiteratureCandidate(source="Crossref", title="A paper", doi="10.1234/ABC.")
    same_doi = LiteratureCandidate(source="OpenAlex", title="Different title", doi="10.1234/abc")
    assert candidate_dedup_key(doi) == "doi:10.1234/abc"
    assert candidate_dedup_key(doi) == candidate_dedup_key(same_doi)


def test_candidate_identity_falls_back_to_title_year_first_author() -> None:
    left = LiteratureCandidate(
        source="arxiv", title="  Dynamic   response ", year=2026, authors=[{"name": "A. Smith"}]
    )
    right = LiteratureCandidate(
        source="semantic", title="Dynamic response", year=2026, authors=[{"name": "A. Smith"}]
    )
    assert candidate_dedup_key(left).startswith("title:")
    assert candidate_dedup_key(left) == candidate_dedup_key(right)


def test_candidate_validation_normalizes_transport_fields() -> None:
    candidate = validate_candidate(
        LiteratureCandidate(source="  OpenAlex ", title="A   title", doi=" 10.1/x. ")
    )
    assert candidate.source == "openalex"
    assert candidate.title == "A title"
    assert candidate.doi == "10.1/x."


def test_search_request_rejects_reversed_year_window() -> None:
    with pytest.raises(ValueError, match="start_year"):
        LiteratureSearchRequest(topic="impact", start_year=2026, end_year=2020)


@pytest.mark.asyncio
async def test_discovery_run_hit_and_source_attempt_are_scoped_and_cascading(app) -> None:
    library_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        library = DirectionLibrary(id=library_id, name="Discovery test", is_public=True)
        session.add(library)
        await session.flush()

        run = LiteratureSearchRun(
            library_id=library.id,
            created_by=None,
            requested_count=20,
            candidate_budget=80,
            topic="impact response",
            query_plan={"queries": ["impact response"]},
            source_config={"semantic": {"enabled": True}},
            progress={"phase": "queued"},
        )
        session.add(run)
        await session.flush()

        session.add(
            LiteratureSourceAttempt(
                run_id=run.id,
                source="semantic",
                requested_count=80,
            )
        )
        session.add(
            LiteratureSearchHit(
                run_id=run.id,
                source="semantic",
                dedup_key="doi:10.1234/example",
                title="Impact response",
                abstract="Abstract",
            )
        )
        await session.commit()

        assert (
            await session.scalar(
                select(LiteratureSearchHit.id)
                .where(LiteratureSearchHit.run_id == run.id)
                .limit(1)
            )
        ) is not None

        await session.execute(delete(LiteratureSearchRun).where(LiteratureSearchRun.id == run.id))
        await session.commit()
        assert (
            await session.scalar(
                select(LiteratureSearchHit.id)
                .where(LiteratureSearchHit.run_id == run.id)
                .limit(1)
            )
        ) is None
        assert (
            await session.scalar(
                select(LiteratureSourceAttempt.id)
                .where(LiteratureSourceAttempt.run_id == run.id)
                .limit(1)
            )
        ) is None
