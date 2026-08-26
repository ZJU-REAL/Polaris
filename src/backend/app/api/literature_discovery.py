"""Library-scoped literature discovery workspace API."""

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.queue import TaskQueue, get_task_queue
from app.core.redis import get_redis_dep
from app.models.library_direction import DirectionLibrary
from app.models.literature_discovery import (
    LiteratureOaCache,
    LiteratureSearchHit,
    LiteratureSearchRun,
    LiteratureSourceAttempt,
)
from app.models.user import User
from app.schemas.literature_discovery import (
    LiteratureSearchRequest,
    OaCacheBatchRequest,
    OaCacheRead,
    PromoteHitsRequest,
    SearchHitPage,
    SearchHitRead,
    SearchRunDetail,
    SearchRunPage,
    SearchRunRead,
    SourceAttemptRead,
)
from app.services import libraries as libraries_service
from app.services.literature import discovery_runs, oa_cache

router = APIRouter(tags=["literature-discovery"])
logger = logging.getLogger(__name__)


async def _library(session: AsyncSession, library_id: uuid.UUID, user: User) -> DirectionLibrary:
    library = await libraries_service.get_library(session, library_id)
    if library is None or not libraries_service.library_visible_to(library, user):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="LIBRARY_NOT_FOUND")
    return library


async def _managed_library(
    session: AsyncSession, library_id: uuid.UUID, user: User
) -> DirectionLibrary:
    library = await _library(session, library_id, user)
    if not await discovery_runs.can_manage_discovery(session, library=library, user=user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="LIBRARY_DISCOVERY_FORBIDDEN")
    return library


def _detail(run: LiteratureSearchRun, attempts: list[LiteratureSourceAttempt]) -> SearchRunDetail:
    return SearchRunDetail(
        **SearchRunRead.model_validate(run).model_dump(),
        source_attempts=[SourceAttemptRead.model_validate(item) for item in attempts],
    )


@router.post(
    "/libraries/{library_id}/literature/runs",
    response_model=SearchRunDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_run(
    library_id: uuid.UUID,
    data: LiteratureSearchRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SearchRunDetail:
    library = await _managed_library(session, library_id, user)
    run = LiteratureSearchRun(
        library_id=library.id,
        created_by=user.id,
        requested_count=data.requested_count,
        candidate_budget=data.candidate_budget,
        start_year=data.start_year,
        end_year=data.end_year,
        topic=data.topic,
        query_plan=data.query_plan,
        source_config=data.source_config,
        model_version=data.model_version,
        progress={"phase": "queued", "fetched": 0, "accepted": 0},
    )
    session.add(run)
    await session.flush()
    for source in discovery_runs.enabled_sources(data.source_config, data.query_plan):
        session.add(
            LiteratureSourceAttempt(
                run_id=run.id,
                source=source,
                status="pending",
                requested_count=data.candidate_budget,
            )
        )
    await session.commit()
    await session.refresh(run)
    attempts = list(
        (
            await session.execute(
                select(LiteratureSourceAttempt)
                .where(LiteratureSourceAttempt.run_id == run.id)
                .order_by(LiteratureSourceAttempt.source)
            )
        )
        .scalars()
        .all()
    )
    return _detail(run, attempts)


@router.post(
    "/libraries/{library_id}/literature/runs/{run_id}/start",
    response_model=SearchRunRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_run(
    library_id: uuid.UUID,
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    queue: TaskQueue = Depends(get_task_queue),
    user: User = Depends(current_active_user),
) -> SearchRunRead:
    """Queue a persisted run without changing its user-requested count."""
    library = await _managed_library(session, library_id, user)
    run = await discovery_runs.get_visible_run(session, library_id=library.id, run_id=run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SEARCH_RUN_NOT_FOUND")
    if run.status != "queued":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="SEARCH_RUN_NOT_QUEUED")
    await queue.enqueue("run_literature_discovery", str(run.id))
    return SearchRunRead.model_validate(run)


@router.get("/libraries/{library_id}/literature/runs", response_model=SearchRunPage)
async def list_runs(
    library_id: uuid.UUID,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SearchRunPage:
    library = await _library(session, library_id, user)
    base = select(LiteratureSearchRun).where(LiteratureSearchRun.library_id == library.id)
    total = await session.scalar(select(func.count()).select_from(base.subquery())) or 0
    runs = list(
        (
            await session.execute(
                base.order_by(LiteratureSearchRun.created_at.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        )
        .scalars()
        .all()
    )
    return SearchRunPage(
        items=[SearchRunRead.model_validate(run) for run in runs],
        total=total,
        page=page,
        size=size,
    )


@router.get("/libraries/{library_id}/literature/runs/{run_id}", response_model=SearchRunDetail)
async def get_run(
    library_id: uuid.UUID,
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SearchRunDetail:
    await _library(session, library_id, user)
    run = await discovery_runs.get_visible_run(session, library_id=library_id, run_id=run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SEARCH_RUN_NOT_FOUND")
    attempts = list(
        (
            await session.execute(
                select(LiteratureSourceAttempt)
                .where(LiteratureSourceAttempt.run_id == run.id)
                .order_by(LiteratureSourceAttempt.source)
            )
        )
        .scalars()
        .all()
    )
    return _detail(run, attempts)


@router.get(
    "/libraries/{library_id}/literature/runs/{run_id}/hits", response_model=SearchHitPage
)
async def list_hits(
    library_id: uuid.UUID,
    run_id: uuid.UUID,
    q: str | None = Query(None, max_length=500),
    source: str | None = Query(None, max_length=64),
    hit_status: str | None = Query(
        None, alias="status", pattern="^(candidate|promoted|dismissed)$"
    ),
    sort: str = Query("relevance", pattern="^(relevance|novelty|impact|recent|title)$"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SearchHitPage:
    await _library(session, library_id, user)
    run = await discovery_runs.get_visible_run(session, library_id=library_id, run_id=run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SEARCH_RUN_NOT_FOUND")
    stmt = select(LiteratureSearchHit).where(LiteratureSearchHit.run_id == run.id)
    if source:
        stmt = stmt.where(LiteratureSearchHit.source == source.strip().lower())
    if hit_status:
        stmt = stmt.where(LiteratureSearchHit.status == hit_status)
    if q:
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(
            LiteratureSearchHit.title.ilike(pattern)
            | LiteratureSearchHit.abstract.ilike(pattern)
            | LiteratureSearchHit.doi.ilike(pattern)
        )
    hits = list((await session.execute(stmt)).scalars().all())
    if sort == "title":
        hits.sort(key=lambda hit: (hit.title.casefold(), str(hit.id)))
    elif sort == "recent":
        hits.sort(key=lambda hit: (hit.created_at, str(hit.id)), reverse=True)
    else:
        score_key = {"relevance": "relevance", "novelty": "novelty", "impact": "impact"}[sort]
        hits.sort(
            key=lambda hit: (
                discovery_runs.score_value(hit, score_key),
                hit.created_at,
                str(hit.id),
            ),
            reverse=True,
        )
    total = len(hits)
    start = (page - 1) * size
    return SearchHitPage(
        items=[SearchHitRead.model_validate(hit) for hit in hits[start : start + size]],
        total=total,
        page=page,
        size=size,
        sort=sort,
    )


@router.post(
    "/libraries/{library_id}/literature/runs/{run_id}/oa-cache",
    response_model=list[OaCacheRead],
    status_code=status.HTTP_202_ACCEPTED,
)
async def cache_oa_pdfs(
    library_id: uuid.UUID,
    run_id: uuid.UUID,
    body: OaCacheBatchRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[OaCacheRead]:
    """Cache OA PDFs for selected discovery candidates; this does not promote them."""
    await _managed_library(session, library_id, user)
    run = await discovery_runs.get_visible_run(session, library_id=library_id, run_id=run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SEARCH_RUN_NOT_FOUND")
    hits = list(
        (
            await session.execute(
                select(LiteratureSearchHit).where(
                    LiteratureSearchHit.run_id == run.id,
                    LiteratureSearchHit.id.in_(body.hit_ids),
                    LiteratureSearchHit.status == "candidate",
                )
            )
        )
        .scalars()
        .all()
    )
    cached = []
    for hit in hits:
        cached.append(await oa_cache.cache_hit_pdf(session, hit))
    await session.commit()
    return [OaCacheRead.model_validate(item) for item in cached]


@router.get(
    "/libraries/{library_id}/literature/runs/{run_id}/oa-cache",
    response_model=list[OaCacheRead],
)
async def list_oa_cache(
    library_id: uuid.UUID,
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[OaCacheRead]:
    await _library(session, library_id, user)
    run = await discovery_runs.get_visible_run(session, library_id=library_id, run_id=run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SEARCH_RUN_NOT_FOUND")
    rows = (
        await session.execute(
            select(LiteratureOaCache)
            .join(LiteratureSearchHit, LiteratureSearchHit.id == LiteratureOaCache.hit_id)
            .where(LiteratureSearchHit.run_id == run.id)
            .order_by(LiteratureOaCache.created_at.desc())
        )
    ).scalars().all()
    return [OaCacheRead.model_validate(item) for item in rows]


@router.post(
    "/libraries/{library_id}/literature/runs/{run_id}/promote",
    response_model=list[SearchHitRead],
    status_code=status.HTTP_201_CREATED,
)
async def promote_hits(
    library_id: uuid.UUID,
    run_id: uuid.UUID,
    body: PromoteHitsRequest,
    session: AsyncSession = Depends(get_session),
    redis=Depends(get_redis_dep),
    user: User = Depends(current_active_user),
) -> list[SearchHitRead]:
    """Promote candidates into the library, then launch the normal enrichment pipeline."""
    library = await _managed_library(session, library_id, user)
    run = await discovery_runs.get_visible_run(session, library_id=library_id, run_id=run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SEARCH_RUN_NOT_FOUND")
    from app.models.paper import Paper, new_paper
    from app.services.dedup import pool_dedup_key
    from app.services.libraries import ensure_membership, find_pool_paper
    from app.services.paper_assets import create_or_reuse_asset, storage_path_for_blob
    from app.services.paper_enrich import launch_paper_enrichment

    hits = list(
        (
            await session.execute(
                select(LiteratureSearchHit).where(
                    LiteratureSearchHit.run_id == run.id,
                    LiteratureSearchHit.id.in_(body.hit_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    promoted: list[LiteratureSearchHit] = []
    task_inputs: list[tuple[uuid.UUID, bool]] = []
    for hit in hits:
        paper = await session.get(Paper, hit.paper_id) if hit.paper_id else None
        if paper is None:
            dedup_key = pool_dedup_key(
                arxiv_id=hit.arxiv_id,
                doi=hit.doi,
                title=hit.title,
                year=hit.year,
                authors=hit.authors,
            )
            paper = await find_pool_paper(
                session, arxiv_id=hit.arxiv_id, doi=hit.doi, dedup_key=dedup_key
            )
            if paper is None:
                paper = new_paper(
                    source=hit.source,
                    dedup_key=dedup_key,
                    arxiv_id=hit.arxiv_id,
                    doi=hit.doi,
                    external_ids=hit.metadata_snapshot,
                    title=hit.title,
                    authors=hit.authors,
                    abstract=hit.abstract,
                    year=hit.year,
                    venue=hit.venue,
                    url=hit.url,
                )
                session.add(paper)
                await session.flush()
        scores = hit.scores if isinstance(hit.scores, dict) else {}
        membership, _ = await ensure_membership(
            session,
            library_id=library.id,
            paper_id=paper.id,
            status="candidate",
            relevance_score=scores.get("relevance"),
            relevance_reason=scores.get("reason") or scores.get("rationale"),
        )
        cache = await session.scalar(
            select(LiteratureOaCache).where(LiteratureOaCache.hit_id == hit.id)
        )
        if cache is not None and cache.status == "ready" and cache.blob_id is not None:
            blob = await session.get(oa_cache.PdfBlob, cache.blob_id)
            if blob is not None:
                path = storage_path_for_blob(blob)
                if path.is_file():
                    content = await asyncio.to_thread(path.read_bytes)
                    identity = f"doi:{hit.doi.lower()}" if hit.doi else (
                        f"arxiv:{hit.arxiv_id.lower()}" if hit.arxiv_id else None
                    )
                    await create_or_reuse_asset(
                        session,
                        paper=paper,
                        library=library,
                        content=content,
                        user=user,
                        source="oa",
                        source_locator=cache.final_url or cache.source_url,
                        identity_key=identity,
                        identity_status="verified" if identity else "unverified",
                        sharing_scope="public",
                    )
        hit.paper_id = paper.id
        hit.status = "promoted"
        hit.promoted_at = datetime.now(UTC)
        promoted.append(hit)
        task_inputs.append((paper.id, True))
    await session.commit()
    for paper_id, _ in task_inputs:
        try:
            await launch_paper_enrichment(
                redis=redis,
                paper_id=paper_id,
                user_id=user.id,
                library_id=library.id,
                project_id=library.project_id,
            )
        except Exception:  # noqa: BLE001 - promotion remains durable if worker is unavailable
            logger.exception("failed to launch enrichment for promoted paper %s", paper_id)
    return [SearchHitRead.model_validate(item) for item in promoted]


@router.post(
    "/libraries/{library_id}/literature/runs/{run_id}/cancel", response_model=SearchRunRead
)
async def cancel_run(
    library_id: uuid.UUID,
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> SearchRunRead:
    library = await _managed_library(session, library_id, user)
    run = await discovery_runs.get_visible_run(session, library_id=library.id, run_id=run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SEARCH_RUN_NOT_FOUND")
    if run.status in {"queued", "running"}:
        run.status = "cancelled"
        run.completed_at = datetime.now(UTC)
        run.progress = {**(run.progress or {}), "phase": "cancelled"}
        await session.commit()
        await session.refresh(run)
    return SearchRunRead.model_validate(run)


@router.delete(
    "/libraries/{library_id}/literature/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_run(
    library_id: uuid.UUID,
    run_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    library = await _managed_library(session, library_id, user)
    run = await discovery_runs.get_visible_run(session, library_id=library.id, run_id=run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="SEARCH_RUN_NOT_FOUND")
    await discovery_runs.delete_run(session, run)
    await session.commit()
