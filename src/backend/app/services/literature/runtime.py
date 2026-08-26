"""Execute persisted library-scoped discovery runs through source adapters.

The runtime keeps provider I/O, normalization, ranking, and persistence in
separate steps. Unpromoted hits never create a Paper or a PDF processing job.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.literature_discovery import (
    LiteratureSearchHit,
    LiteratureSearchRun,
    LiteratureSourceAttempt,
)
from app.schemas.literature_discovery import (
    LiteratureCandidate,
    SourceAdapter,
    SourceSearchPage,
    SourceSearchRequest,
)
from app.services.literature import discovery_runs
from app.services.literature.discovery import candidate_dedup_key, validate_candidate
from app.services.literature.discovery_ranking import rank_candidates

logger = logging.getLogger(__name__)


class SourceExecutionError(RuntimeError):
    """A provider failure with a persisted, user-readable category."""

    def __init__(self, code: str, detail: str, *, retryable: bool = False) -> None:
        super().__init__(detail)
        self.code = code
        self.retryable = retryable


class AdapterRegistry:
    """Small dependency-injection registry used by workers and tests."""

    def __init__(self, adapters: Sequence[SourceAdapter] = ()) -> None:
        self._adapters = {adapter.name.strip().lower(): adapter for adapter in adapters}

    def get(self, source: str) -> SourceAdapter | None:
        return self._adapters.get(source.strip().lower())

    def names(self) -> set[str]:
        return set(self._adapters)


class OpenAlexAdapter:
    name = "openalex"

    def __init__(self, client: Any) -> None:
        self.client = client

    async def search(self, request: SourceSearchRequest) -> SourceSearchPage:
        rows = await self.client.search_works(request.query, limit=request.limit)
        return SourceSearchPage(
            source=self.name,
            items=[_candidate_from_openalex(row) for row in rows],
            fetched_count=len(rows),
        )


class SemanticScholarAdapter:
    name = "semantic"

    def __init__(self, client: Any) -> None:
        self.client = client

    async def search(self, request: SourceSearchRequest) -> SourceSearchPage:
        rows = await self.client.search_papers(request.query, limit=request.limit)
        return SourceSearchPage(
            source=self.name,
            items=[_candidate_from_semantic(row) for row in rows],
            fetched_count=len(rows),
        )


class ArxivAdapter:
    name = "arxiv"

    def __init__(self, client: Any) -> None:
        self.client = client

    async def search(self, request: SourceSearchRequest) -> SourceSearchPage:
        since = datetime(request.start_year, 1, 1, tzinfo=UTC) if request.start_year else None
        until = datetime(request.end_year, 12, 31, 23, 59, tzinfo=UTC) if request.end_year else None
        rows = await self.client.search(
            keywords=[request.query], since=since, until=until, limit=request.limit
        )
        return SourceSearchPage(
            source=self.name,
            items=[_candidate_from_arxiv(row) for row in rows],
            fetched_count=len(rows),
        )


def _candidate_from_openalex(row: Mapping[str, Any]) -> LiteratureCandidate:
    return validate_candidate(
        LiteratureCandidate(
            source="openalex",
            title=str(row.get("title") or "Untitled"),
            abstract=row.get("abstract"),
            authors=row.get("authors") or [],
            year=row.get("year"),
            venue=row.get("venue"),
            doi=row.get("doi"),
            url=row.get("url"),
            citation_count=row.get("cited_by_count"),
            metadata=dict(row),
        )
    )


def _candidate_from_semantic(row: Mapping[str, Any]) -> LiteratureCandidate:
    external = row.get("externalIds") or {}
    return validate_candidate(
        LiteratureCandidate(
            source="semantic",
            title=str(row.get("title") or "Untitled"),
            abstract=row.get("abstract"),
            authors=[a for a in row.get("authors") or [] if isinstance(a, Mapping)],
            year=row.get("year"),
            venue=row.get("venue"),
            doi=external.get("DOI"),
            arxiv_id=external.get("ArXiv"),
            semantic_scholar_id=row.get("paperId"),
            url=row.get("url"),
            citation_count=row.get("citationCount"),
            metadata=dict(row),
        )
    )


def _candidate_from_arxiv(row: Mapping[str, Any]) -> LiteratureCandidate:
    return validate_candidate(
        LiteratureCandidate(
            source="arxiv",
            title=str(row.get("title") or "Untitled"),
            abstract=row.get("abstract"),
            authors=row.get("authors") or [],
            year=row.get("year"),
            doi=row.get("doi"),
            arxiv_id=row.get("arxiv_id"),
            url=row.get("url"),
            pdf_url=row.get("pdf_url"),
            oa_status="oa" if row.get("pdf_url") else None,
            metadata=dict(row),
        )
    )


def _config_values(run: LiteratureSearchRun) -> tuple[list[str], list[str], dict[str, float]]:
    config = run.source_config if isinstance(run.source_config, dict) else {}
    sources = discovery_runs.enabled_sources(run.source_config, run.query_plan)
    keywords = [str(v) for v in config.get("keywords") or [] if str(v).strip()]
    weights = config.get("score_weights")
    return sources, keywords, weights if isinstance(weights, dict) else {}


def _planned_query(run: LiteratureSearchRun, source: str, keywords: Sequence[str]) -> str:
    plan = run.query_plan if isinstance(run.query_plan, dict) else {}
    queries = plan.get("queries")
    if isinstance(queries, list):
        for item in queries:
            if isinstance(item, dict) and str(item.get("source", "")).lower() == source:
                query = str(item.get("query") or "").strip()
                if query:
                    return query
    return str(plan.get("query") or run.topic or (keywords[0] if keywords else "")).strip()


async def _default_registry() -> AdapterRegistry:
    from app.services.literature.arxiv import ArxivClient
    from app.services.literature.openalex import OpenAlexClient
    from app.services.literature.semantic_scholar import SemanticScholarClient

    return AdapterRegistry(
        (
            OpenAlexAdapter(OpenAlexClient()),
            SemanticScholarAdapter(SemanticScholarClient()),
            ArxivAdapter(ArxivClient()),
        )
    )


async def run_discovery(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    registry: AdapterRegistry | None = None,
    now: datetime | None = None,
) -> LiteratureSearchRun:
    """Execute one persisted run and return its final state."""

    run = await session.get(LiteratureSearchRun, run_id)
    if run is None:
        raise ValueError(f"search run not found: {run_id}")
    if run.status == "cancelled":
        return run

    registry = registry or await _default_registry()
    started = now or datetime.now(UTC)
    run.status = "running"
    run.started_at = run.started_at or started
    run.progress = {**(run.progress or {}), "phase": "retrieving", "fetched": 0, "accepted": 0}
    await session.commit()

    sources, keywords, weights = _config_values(run)
    if not sources:
        run.status = "failed"
        run.error_summary = "NO_SOURCES_CONFIGURED"
        run.completed_at = started
        run.progress = {
            **(run.progress or {}),
            "phase": "failed",
            "source": None,
            "fetched": 0,
            "accepted": 0,
            "requested_count": run.requested_count,
            "candidate_budget": run.candidate_budget,
            "returned_count": 0,
        }
        await session.commit()
        await session.refresh(run)
        return run
    attempts = list(
        (
            await session.execute(
                select(LiteratureSourceAttempt).where(LiteratureSourceAttempt.run_id == run.id)
            )
        )
        .scalars()
        .all()
    )
    attempt_by_source = {attempt.source.lower(): attempt for attempt in attempts}
    candidates: list[LiteratureCandidate] = []
    failures: list[str] = []
    fetched_total = 0

    for source in sources:
        attempt = attempt_by_source.get(source)
        if attempt is None:
            attempt = LiteratureSourceAttempt(run_id=run.id, source=source)
            session.add(attempt)
        adapter = registry.get(source)
        if adapter is None:
            attempt.status = "skipped"
            attempt.error_code = "SOURCE_NOT_CONFIGURED"
            attempt.error_detail = "No adapter is configured for this source"
            failures.append(f"{source}: SOURCE_NOT_CONFIGURED")
            run.progress = {
                **(run.progress or {}),
                "phase": "retrieving",
                "source": source,
                "fetched": fetched_total,
                "accepted": len(candidates),
                "requested_count": run.requested_count,
                "candidate_budget": run.candidate_budget,
            }
            await session.commit()
            continue

        query = _planned_query(run, source, keywords)
        attempt.status = "running"
        attempt.query = query
        attempt.requested_count = run.candidate_budget
        attempt.started_at = datetime.now(UTC)
        await session.commit()
        try:
            page = await adapter.search(
                SourceSearchRequest(
                    query=query,
                    start_year=run.start_year,
                    end_year=run.end_year,
                    limit=run.candidate_budget,
                )
            )
            validated = [validate_candidate(item) for item in page.items]
            candidates.extend(validated)
            fetched_total += page.fetched_count
            attempt.fetched_count = page.fetched_count
            attempt.accepted_count = len(validated)
            attempt.cursor = page.next_cursor
            attempt.status = "completed"
            attempt.completed_at = datetime.now(UTC)
        except Exception as exc:  # noqa: BLE001 - provider isolation is intentional
            logger.warning("literature source failed: %s", source, exc_info=True)
            error = exc if isinstance(exc, SourceExecutionError) else SourceExecutionError(
                "SOURCE_REQUEST_FAILED", str(exc), retryable=True
            )
            attempt.status = "partial" if candidates else "failed"
            attempt.retryable = error.retryable
            attempt.error_code = error.code
            attempt.error_detail = str(error)
            attempt.completed_at = datetime.now(UTC)
            failures.append(f"{source}: {error.code}")
        run.progress = {
            **(run.progress or {}),
            "phase": "retrieving",
            "source": source,
            "fetched": fetched_total,
            "accepted": len(candidates),
            "requested_count": run.requested_count,
            "candidate_budget": run.candidate_budget,
        }
        await session.commit()

    ranked = rank_candidates(
        [candidate.model_dump() for candidate in candidates],
        topic=run.topic,
        keywords=keywords,
        excluded_keywords=(run.source_config or {}).get("excluded_keywords", [])
        if isinstance(run.source_config, dict)
        else (),
        weights=weights,
        current_year=(now or datetime.now(UTC)).year,
        limit=run.requested_count,
    )
    for item in ranked:
        # ``sources`` and ``retrieval_hits`` are ranking metadata, not part of the
        # strict candidate DTO. Read them from the ranked mapping before validation
        # so cross-source provenance survives persistence.
        raw_candidate = item.candidate
        candidate = validate_candidate(LiteratureCandidate.model_validate(raw_candidate))
        session.add(
            LiteratureSearchHit(
                run_id=run.id,
                status="candidate",
                source=candidate.source,
                dedup_key=item.identity or candidate_dedup_key(candidate),
                title=candidate.title,
                abstract=candidate.abstract,
                authors=candidate.authors,
                year=candidate.year,
                venue=candidate.venue,
                doi=candidate.doi,
                pmid=candidate.pmid,
                arxiv_id=candidate.arxiv_id,
                semantic_scholar_id=candidate.semantic_scholar_id,
                url=candidate.url,
                pdf_url=candidate.pdf_url,
                oa_status=candidate.oa_status,
                citation_count=candidate.citation_count,
                scores={
                    **item.dimensions,
                    "overall": item.score,
                    "tier": item.tier,
                    "reasons": list(item.reasons),
                },
                metadata_snapshot={
                    **(candidate.metadata or {}),
                    "sources": list(raw_candidate.get("sources") or [candidate.source]),
                    "retrieval_hits": list(raw_candidate.get("retrieval_hits") or []),
                },
            )
        )

    run.status = "partial" if failures and ranked else "failed" if failures else "completed"
    run.error_summary = "; ".join(failures) if failures else None
    run.completed_at = datetime.now(UTC)
    run.progress = {
        **(run.progress or {}),
        "phase": "completed" if run.status != "failed" else "failed",
        "fetched": fetched_total,
        "accepted": len(ranked),
        "requested_count": run.requested_count,
        "candidate_budget": run.candidate_budget,
        "returned_count": len(ranked),
    }
    await session.commit()
    await session.refresh(run)
    return run
