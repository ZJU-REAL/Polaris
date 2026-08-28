"""Execute persisted library-scoped discovery runs through source adapters.

The runtime keeps provider I/O, normalization, ranking, and persistence in
separate steps. Unpromoted hits never create a Paper or a PDF processing job.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.llm.router import get_llm_router
from app.models.library_direction import LibraryPaper
from app.models.literature_discovery import (
    LiteratureSearchHit,
    LiteratureSearchRun,
    LiteratureSourceAttempt,
)
from app.models.paper import Paper
from app.schemas.literature_discovery import (
    LiteratureCandidate,
    SourceAdapter,
    SourceSearchPage,
    SourceSearchRequest,
)
from app.services import literature_settings
from app.services.interdisciplinary_retrieval import rerank_interdisciplinary
from app.services.literature import discovery_runs
from app.services.literature.discovery import candidate_dedup_key, validate_candidate
from app.services.literature.discovery_ranking import SCORING_VERSION, rank_candidates
from app.services.literature.multi_source import MultiSourceClient, ProviderRequestError
from app.services.literature.retrieval_quality import (
    ExecutableQuery,
    QueryGenerationError,
    add_retrieval_hit,
    allocate_query_budget,
    fuse_candidates,
    generate_query_families,
    model_rerank,
)

logger = logging.getLogger(__name__)
_REGISTRY_CACHE: tuple[str, AdapterRegistry] | None = None
_REGISTRY_LOCK = asyncio.Lock()
_ROTATION_LOCK = threading.Lock()
_ROTATION_INDEX: dict[str, int] = {}


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


class RotatingAdapter:
    """Select one configured credential without persisting it in a run."""

    def __init__(self, name: str, adapters: Sequence[SourceAdapter]) -> None:
        if not adapters:
            raise ValueError("at least one adapter is required")
        self.name = name
        self._adapters = tuple(adapters)

    async def search(self, request: SourceSearchRequest) -> SourceSearchPage:
        with _ROTATION_LOCK:
            index = _ROTATION_INDEX.get(self.name, 0)
            _ROTATION_INDEX[self.name] = index + 1
        return await self._adapters[index % len(self._adapters)].search(request)


class OpenAlexAdapter:
    name = "openalex"

    def __init__(self, client: Any) -> None:
        self.client = client

    async def search(self, request: SourceSearchRequest) -> SourceSearchPage:
        rows = await self.client.search_works(
            request.query,
            limit=request.limit,
            start_year=request.start_year,
            end_year=request.end_year,
        )
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
        rows = await self.client.search_papers(
            request.query,
            limit=request.limit,
            start_year=request.start_year,
            end_year=request.end_year,
        )
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
        if hasattr(self.client, "search_raw"):
            rows = await self.client.search_raw(
                request.query, since=since, until=until, limit=request.limit
            )
        else:
            rows = await self.client.search(
                keywords=[request.query], since=since, until=until, limit=request.limit
            )
        return SourceSearchPage(
            source=self.name,
            items=[_candidate_from_arxiv(row) for row in rows],
            fetched_count=len(rows),
        )


class MultiSourceAdapter:
    """Adapter for providers that share the normalized YFR-compatible client."""

    def __init__(self, name: str, client: MultiSourceClient) -> None:
        self.name = name
        self.client = client

    async def search(self, request: SourceSearchRequest) -> SourceSearchPage:
        rows = await self.client.search_source(self.name, request)
        return SourceSearchPage(
            source=self.name,
            items=[validate_candidate(_candidate_from_generic(self.name, row)) for row in rows],
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


def _candidate_from_generic(source: str, row: Mapping[str, Any]) -> LiteratureCandidate:
    return LiteratureCandidate(
        source=source,
        title=str(row.get("title") or "Untitled"),
        abstract=row.get("abstract"),
        authors=row.get("authors") or [],
        year=row.get("year"),
        venue=row.get("venue"),
        doi=row.get("doi"),
        pmid=row.get("pmid"),
        url=row.get("url"),
        pdf_url=row.get("pdf_url"),
        oa_status=row.get("oa_status"),
        citation_count=row.get("citation_count"),
        metadata=dict(row.get("metadata") or row),
    )


def _config_values(run: LiteratureSearchRun) -> tuple[list[str], list[str], dict[str, float]]:
    config = run.source_config if isinstance(run.source_config, dict) else {}
    sources = discovery_runs.enabled_sources(run.source_config, run.query_plan)
    raw_keywords = config.get("keywords")
    keywords = (
        [str(v) for v in raw_keywords if str(v).strip()] if isinstance(raw_keywords, list) else []
    )
    weights = config.get("score_weights")
    return sources, keywords, weights if isinstance(weights, dict) else {}


def _credential_pool(settings: Mapping[str, Any], source: str, fallback: str = "") -> list[str]:
    configured = settings.get("provider_keys")
    declared = isinstance(configured, Mapping) and source in configured
    values = configured.get(source) if declared else None
    pool = [str(value).strip() for value in values or [] if str(value).strip()]
    if declared:
        return pool
    return [item for value in fallback.replace(";", ",").split(",") if (item := value.strip())]


def _registry_fingerprint(settings: Mapping[str, Any]) -> str:
    payload = json.dumps(settings, sort_keys=True, ensure_ascii=True, default=str).encode()
    return hashlib.sha256(payload).hexdigest()


async def build_adapter_registry(runtime_settings: Mapping[str, Any]) -> AdapterRegistry:
    """Build or reuse adapters from trusted, decrypted administrator settings."""
    global _REGISTRY_CACHE
    fingerprint = _registry_fingerprint(runtime_settings)
    async with _REGISTRY_LOCK:
        if _REGISTRY_CACHE is not None and _REGISTRY_CACHE[0] == fingerprint:
            return _REGISTRY_CACHE[1]

        app_settings = get_settings()
        openalex_keys = _credential_pool(runtime_settings, "openalex") or [""]
        semantic_keys = _credential_pool(runtime_settings, "semantic", app_settings.s2_api_key) or [
            ""
        ]

        from app.services.literature.arxiv import ArxivClient
        from app.services.literature.openalex import OpenAlexClient
        from app.services.literature.semantic_scholar import SemanticScholarClient

        multi_source = MultiSourceClient(
            provider_keys=runtime_settings.get("provider_keys")
            if isinstance(runtime_settings.get("provider_keys"), Mapping)
            else None
        )
        registry = AdapterRegistry(
            (
                RotatingAdapter(
                    "openalex",
                    [OpenAlexAdapter(OpenAlexClient(api_key=key or None)) for key in openalex_keys],
                ),
                RotatingAdapter(
                    "semantic",
                    [
                        SemanticScholarAdapter(SemanticScholarClient(api_key=key or None))
                        for key in semantic_keys
                    ],
                ),
                ArxivAdapter(ArxivClient()),
                *(
                    MultiSourceAdapter(source, multi_source)
                    for source in (
                        "pubmed",
                        "crossref",
                        "europepmc",
                        "hal",
                        "core",
                        "base",
                        "sciverse",
                        "unpaywall",
                    )
                ),
            )
        )
        _REGISTRY_CACHE = (fingerprint, registry)
        return registry


async def run_discovery(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    registry: AdapterRegistry | None = None,
    llm_router: Any | None = None,
    venue_metric_service: Any | None = None,
    now: datetime | None = None,
) -> LiteratureSearchRun:
    """Execute one persisted run and return its final state."""

    run = await session.scalar(
        select(LiteratureSearchRun)
        .where(LiteratureSearchRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise ValueError(f"search run not found: {run_id}")
    if run.status != "queued":
        return run

    owns_registry = registry is None
    runtime_settings: Mapping[str, Any] | None = None
    if owns_registry:
        runtime_settings = await literature_settings.get_runtime_settings(session)
        registry = await build_adapter_registry(runtime_settings)
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
    llm_router = llm_router or get_llm_router()
    source_config = run.source_config if isinstance(run.source_config, dict) else {}
    raw_exclusions = source_config.get("excluded_keywords")
    excluded_keywords = (
        [str(item) for item in raw_exclusions if str(item).strip()]
        if isinstance(raw_exclusions, list)
        else []
    )
    score_rubric = source_config.get("score_rubric")
    try:
        families, generation_snapshot = await generate_query_families(
            llm_router=llm_router,
            topic=run.topic,
            keywords=keywords,
            excluded_keywords=excluded_keywords,
            score_rubric=score_rubric,
            query_plan=run.query_plan,
            user_id=run.created_by,
            library_id=run.library_id,
        )
    except QueryGenerationError:
        run.status = "failed"
        run.error_summary = "QUERY_GENERATION_FAILED"
        run.completed_at = datetime.now(UTC)
        run.progress = {
            **(run.progress or {}),
            "phase": "failed",
            "error_code": "QUERY_GENERATION_FAILED",
            "requested_count": run.requested_count,
            "candidate_budget": run.candidate_budget,
            "returned_count": 0,
        }
        await session.commit()
        await session.refresh(run)
        return run
    tasks = allocate_query_budget(
        sources=sources,
        families=families,
        candidate_budget=run.candidate_budget,
    )
    plan_snapshot = dict(run.query_plan or {})
    plan_snapshot.update(
        {
            "version": generation_snapshot["version"],
            "query_generation": generation_snapshot,
            "queries": [task.snapshot() for task in tasks],
            "candidate_budget": run.candidate_budget,
            "start_year": run.start_year,
            "end_year": run.end_year,
        }
    )
    run.query_plan = plan_snapshot
    if run.model_version is None:
        run.model_version = generation_snapshot.get("model")
    run.progress = {
        **(run.progress or {}),
        "phase": "retrieving",
        "query_total": len(tasks),
        "query_completed": 0,
        "requested_count": run.requested_count,
        "candidate_budget": run.candidate_budget,
        "start_year": run.start_year,
        "end_year": run.end_year,
    }
    await session.commit()
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
    candidates: list[dict[str, Any]] = []
    failures: list[str] = []
    fetched_total = 0

    tasks_by_source: dict[str, list[ExecutableQuery]] = {source: [] for source in sources}
    for task in tasks:
        tasks_by_source.setdefault(task.source, []).append(task)
    runnable: list[tuple[ExecutableQuery, Any, LiteratureSourceAttempt]] = []
    for source in sources:
        attempt = attempt_by_source.get(source)
        if attempt is None:
            attempt = LiteratureSourceAttempt(run_id=run.id, source=source)
            session.add(attempt)
            attempt_by_source[source] = attempt
        source_tasks = tasks_by_source.get(source, [])
        if not source_tasks:
            attempt.status = "skipped"
            attempt.error_code = "BUDGET_NOT_ALLOCATED"
            attempt.error_detail = "Aggregate candidate budget did not allocate this source"
            continue
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

        attempt.status = "running"
        attempt.query = "\n".join(task.query for task in source_tasks)
        attempt.requested_count = sum(task.limit for task in source_tasks)
        attempt.started_at = datetime.now(UTC)
        attempt.metadata_snapshot = {
            "queries": [task.snapshot() for task in source_tasks],
            "start_year": run.start_year,
            "end_year": run.end_year,
        }
        runnable.extend((task, adapter, attempt) for task in source_tasks)

    await session.commit()

    semaphore = asyncio.Semaphore(get_settings().literature_source_concurrency)

    async def fetch(
        task_index: int, task: ExecutableQuery, adapter: Any
    ) -> tuple[int, SourceSearchPage | None, SourceExecutionError | None]:
        async with semaphore:
            try:
                page = await adapter.search(
                    SourceSearchRequest(
                        query=task.query,
                        start_year=run.start_year,
                        end_year=run.end_year,
                        limit=task.limit,
                    )
                )
                return task_index, page, None
            except ProviderRequestError as exc:
                return (
                    task_index,
                    None,
                    SourceExecutionError(
                        exc.code, f"{task.source} provider request failed", retryable=exc.retryable
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - provider isolation is intentional
                return (
                    task_index,
                    None,
                    SourceExecutionError(
                        "SOURCE_REQUEST_FAILED",
                        f"{task.source} provider raised {type(exc).__name__}",
                        retryable=True,
                    ),
                )

    source_stats: dict[str, dict[str, Any]] = {
        source: {"fetched": 0, "accepted": 0, "succeeded": 0, "failed": []} for source in sources
    }
    accepted_total = 0
    candidates_by_query: dict[int, list[dict[str, Any]]] = {}
    pending = [
        asyncio.create_task(fetch(task_index, task, adapter))
        for task_index, (task, adapter, _) in enumerate(runnable)
    ]
    for completed_count, completed in enumerate(asyncio.as_completed(pending), start=1):
        task_index, page, source_error = await completed
        task, _, attempt = runnable[task_index]
        source = task.source
        stats = source_stats[source]
        try:
            if source_error is not None:
                raise source_error
            assert page is not None
            validated = [validate_candidate(item) for item in page.items]
            filtered = [
                item
                for item in validated
                if (run.start_year is None or item.year is None or item.year >= run.start_year)
                and (run.end_year is None or item.year is None or item.year <= run.end_year)
            ]
            query_candidates = list(
                add_retrieval_hit(item.model_dump(), task=task, rank=rank)
                for rank, item in enumerate(filtered, start=1)
            )
            candidates_by_query[task_index] = query_candidates
            fetched_total += page.fetched_count
            accepted_total += len(query_candidates)
            stats["fetched"] += page.fetched_count
            stats["accepted"] += len(filtered)
            stats["succeeded"] += 1
            attempt.cursor = page.next_cursor or attempt.cursor
        except Exception as exc:  # noqa: BLE001 - provider isolation is intentional
            logger.warning("literature source failed: %s", source, exc_info=True)
            error = (
                exc
                if isinstance(exc, SourceExecutionError)
                else SourceExecutionError(
                    "SOURCE_REQUEST_FAILED",
                    f"candidate processing raised {type(exc).__name__}",
                    retryable=True,
                )
            )
            attempt.retryable = error.retryable
            attempt.error_code = error.code
            attempt.error_detail = str(error)
            stats["failed"].append(error.code)
        run.progress = {
            **(run.progress or {}),
            "phase": "retrieving",
            "source": source,
            "query_purpose": task.purpose,
            "query_index": task_index + 1,
            "query_total": len(runnable),
            "query_completed": completed_count,
            "fetched": fetched_total,
            "accepted": accepted_total,
            "requested_count": run.requested_count,
            "candidate_budget": run.candidate_budget,
        }
        await session.commit()

    candidates = [
        candidate
        for task_index in range(len(runnable))
        for candidate in candidates_by_query.get(task_index, [])
    ]

    for source in sources:
        attempt = attempt_by_source.get(source)
        if attempt is None or attempt.status == "skipped":
            continue
        stats = source_stats[source]
        attempt.fetched_count = stats["fetched"]
        attempt.accepted_count = stats["accepted"]
        if stats["failed"]:
            attempt.status = "partial" if stats["succeeded"] else "failed"
            failures.append(f"{source}: {','.join(stats['failed'])}")
        else:
            attempt.status = "completed"
        attempt.completed_at = datetime.now(UTC)
    await session.commit()

    reference_rows = (
        await session.execute(
            select(Paper.title, Paper.abstract)
            .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
            .where(
                LibraryPaper.library_id == run.library_id,
                LibraryPaper.status.in_(("scored", "fetched", "compiled", "included")),
            )
            .limit(2000)
        )
    ).all()
    reference_texts = [
        "\n".join(part for part in (title, abstract) if part) for title, abstract in reference_rows
    ]
    fused = fuse_candidates(candidates, executed_query_count=len(runnable))
    historical_duplicates = 0
    if run.trigger == "scheduled":
        from app.services.literature.incremental_filter import filter_known_candidates

        fused, historical_duplicates = await filter_known_candidates(
            session,
            library_id=run.library_id,
            candidates=fused,
        )
    owns_metric_service = venue_metric_service is None and owns_registry
    if owns_metric_service:
        from app.services.literature.venue_metrics import build_venue_metric_service

        venue_metric_service = build_venue_metric_service(runtime_settings or {})
    if venue_metric_service is not None:
        try:
            fused = await venue_metric_service.enrich_candidates(session, fused)
        except Exception:  # metrics must never make discovery candidates unusable
            logger.warning("venue metric enrichment failed", exc_info=True)
        finally:
            if owns_metric_service and hasattr(venue_metric_service, "aclose"):
                await venue_metric_service.aclose()
    run.progress = {
        **(run.progress or {}),
        "phase": "ranking",
        "fetched": fetched_total,
        "accepted": len(candidates),
        "deduplicated": len(fused),
        "historical_duplicates": historical_duplicates,
        "pending_rerank": len(fused),
        "query_completed": len(runnable),
        "query_total": len(runnable),
        "requested_count": run.requested_count,
        "candidate_budget": run.candidate_budget,
        "start_year": run.start_year,
        "end_year": run.end_year,
    }
    await session.commit()
    ranked = rank_candidates(
        fused,
        topic=run.topic,
        keywords=keywords,
        excluded_keywords=excluded_keywords,
        weights=weights,
        current_year=(now or datetime.now(UTC)).year,
        reference_texts=reference_texts,
        # 先排一个更宽的池子，跨学科重排要在里面挑平衡，再截到请求量。
        limit=min(len(fused), max(run.requested_count, run.requested_count * 3)),
    )
    ranked, ranking_snapshot = await model_rerank(
        llm_router=llm_router,
        topic=run.topic,
        score_rubric=score_rubric,
        ranked=ranked,
        weights=weights,
        limit=len(ranked),
        user_id=run.created_by,
        library_id=run.library_id,
    )
    run.progress = {
        **(run.progress or {}),
        "phase": "ranking",
        "pending_rerank": 0,
        "ranked_count": len(ranked),
    }
    await session.commit()
    plan_snapshot = dict(run.query_plan or {})
    plan_snapshot["ranking"] = ranking_snapshot
    run.query_plan = plan_snapshot
    ranked = rerank_interdisciplinary(ranked, query_plan=run.query_plan, limit=run.requested_count)
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
                    "scoring_version": SCORING_VERSION,
                    "ranking_mode": ranking_snapshot["mode"],
                    "ranking_model": ranking_snapshot.get("model"),
                    "open_access": bool(
                        candidate.pdf_url
                        or candidate.oa_status
                        or (
                            candidate.metadata.get("is_oa")
                            if isinstance(candidate.metadata, dict)
                            else False
                        )
                    ),
                    "pdf_cached": False,
                },
                venue_metric_snapshot=raw_candidate.get("venue_metric_snapshot"),
                metadata_snapshot={
                    **(candidate.metadata or {}),
                    "sources": list(raw_candidate.get("sources") or [candidate.source]),
                    "retrieval_hits": list((candidate.metadata or {}).get("retrieval_hits") or []),
                },
            )
        )

    # Cache explicit OA PDFs while hits are still candidates.  This never
    # creates a Paper, asset, parse job, or vector; promotion remains the gate.
    await session.flush()
    from app.services.literature.oa_cache import cache_hit_pdf

    oa_hits = list(
        (
            await session.execute(
                select(LiteratureSearchHit).where(
                    LiteratureSearchHit.run_id == run.id,
                    or_(
                        LiteratureSearchHit.pdf_url.is_not(None),
                        LiteratureSearchHit.doi.is_not(None),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for hit in oa_hits:
        try:
            await cache_hit_pdf(session, hit)
        except Exception:  # noqa: BLE001 - metadata results must survive OA failures
            logger.warning("OA pre-cache failed for hit %s", hit.id, exc_info=True)

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
        "historical_duplicates": historical_duplicates,
        "ranking_mode": ranking_snapshot["mode"],
        "start_year": run.start_year,
        "end_year": run.end_year,
    }
    await session.commit()
    await session.refresh(run)
    return run
