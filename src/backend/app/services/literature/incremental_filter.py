"""Filter candidates already seen by a library's incremental discovery history."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import LibraryPaper
from app.models.literature_discovery import LiteratureSearchHit, LiteratureSearchRun
from app.models.paper import Paper
from app.services.literature.discovery_ranking import candidate_identity

_QUERY_BATCH_SIZE = 250


def _identity(candidate: Mapping[str, Any]) -> str:
    return candidate_identity(candidate)


def _paper_aliases(
    *,
    doi: str | None,
    arxiv_id: str | None,
    title: str,
    year: int | None,
    authors: list[Any] | None,
) -> set[str]:
    aliases: set[str] = set()
    if doi:
        aliases.add(_identity({"doi": doi}))
    if arxiv_id:
        aliases.add(_identity({"arxiv_id": arxiv_id}))
    if title:
        aliases.add(_identity({"title": title, "year": year, "authors": authors or []}))
    return aliases


def _batches(values: Sequence[Any], size: int = _QUERY_BATCH_SIZE):
    for start in range(0, len(values), size):
        yield values[start : start + size]


async def filter_known_candidates(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Return unseen candidates and the number removed from this library's feed."""

    rows = [dict(candidate) for candidate in candidates]
    if not rows:
        return rows, 0
    identity_by_index = [_identity(candidate) for candidate in rows]
    identities = set(identity_by_index)
    known: set[str] = set()
    for identity_batch in _batches(list(identities)):
        known.update(
            (
                await session.execute(
                    select(LiteratureSearchHit.dedup_key)
                    .join(
                        LiteratureSearchRun,
                        LiteratureSearchRun.id == LiteratureSearchHit.run_id,
                    )
                    .where(
                        LiteratureSearchRun.library_id == library_id,
                        LiteratureSearchHit.dedup_key.in_(identity_batch),
                    )
                )
            )
            .scalars()
            .all()
        )

    for candidate_batch in _batches(rows):
        batch_identities = {_identity(candidate) for candidate in candidate_batch}
        dois = {
            key.removeprefix("doi:") for key in batch_identities if key.startswith("doi:")
        }
        arxiv_ids = {
            key.removeprefix("arxiv:")
            for key in batch_identities
            if key.startswith("arxiv:")
        }
        titles = {
            str(candidate.get("title") or "").casefold().strip()
            for candidate in candidate_batch
            if str(candidate.get("title") or "").strip()
        }
        clauses = []
        if dois:
            clauses.append(func.lower(Paper.doi).in_(dois))
        if arxiv_ids:
            clauses.append(func.lower(Paper.arxiv_id).in_(arxiv_ids))
        if titles:
            clauses.append(func.lower(Paper.title).in_(titles))
        if not clauses:
            continue
        paper_rows = (
            await session.execute(
                select(Paper.doi, Paper.arxiv_id, Paper.title, Paper.year, Paper.authors)
                .join(LibraryPaper, LibraryPaper.paper_id == Paper.id)
                .where(LibraryPaper.library_id == library_id, or_(*clauses))
            )
        ).all()
        for paper in paper_rows:
            known.update(
                _paper_aliases(
                    doi=paper.doi,
                    arxiv_id=paper.arxiv_id,
                    title=paper.title,
                    year=paper.year,
                    authors=paper.authors,
                )
            )

    unseen = [
        candidate
        for candidate, identity in zip(rows, identity_by_index, strict=True)
        if identity not in known
    ]
    return unseen, len(rows) - len(unseen)
