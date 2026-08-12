"""Normalization and persistence helpers for global paper identifiers."""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper import Paper, PaperIdentifier
from app.services.literature.arxiv import normalize_arxiv_id
from app.services.literature.contracts import Identifier, ProviderRecord

_DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)


def normalize_identifier(namespace: str, value: str) -> Identifier:
    namespace = namespace.strip().casefold().replace("semantic_scholar", "s2")
    if namespace == "corpus_id":
        namespace = "s2_corpus"
    raw = value.strip()
    if namespace == "arxiv":
        normalized = normalize_arxiv_id(raw).casefold()
    elif namespace == "doi":
        normalized = _DOI_PREFIX_RE.sub("", raw).strip().casefold()
    elif namespace == "pmcid":
        normalized = raw.upper()
        if normalized and not normalized.startswith("PMC"):
            normalized = f"PMC{normalized}"
    elif namespace in {"pmid", "s2_corpus"}:
        normalized = raw.strip()
    else:
        normalized = raw.casefold()
    if not namespace or not normalized:
        raise ValueError("identifier namespace and value must be non-empty")
    return Identifier(namespace=namespace, value=normalized)


def identifiers_from_fields(fields: dict[str, Any]) -> list[Identifier]:
    values: dict[str, str] = {}
    for namespace, value in (fields.get("external_ids") or {}).items():
        if value is not None and str(value).strip():
            values[str(namespace)] = str(value)
    if fields.get("arxiv_id"):
        values["arxiv"] = str(fields["arxiv_id"])
    if fields.get("doi"):
        values["doi"] = str(fields["doi"])
    result: list[Identifier] = []
    seen: set[tuple[str, str]] = set()
    for namespace, value in values.items():
        identifier = normalize_identifier(namespace, value)
        key = (identifier.namespace, identifier.value)
        if key not in seen:
            seen.add(key)
            result.append(identifier)
    return result


async def find_paper_id_by_identifiers(
    session: AsyncSession, identifiers: list[Identifier]
) -> uuid.UUID | None:
    normalized = [normalize_identifier(item.namespace, item.value) for item in identifiers]
    if not normalized:
        return None
    clauses = [
        (PaperIdentifier.namespace == item.namespace)
        & (PaperIdentifier.normalized_value == item.value)
        for item in normalized
    ]
    stmt = select(PaperIdentifier.paper_id).where(or_(*clauses))
    paper_ids = set((await session.execute(stmt)).scalars().all())
    if len(paper_ids) > 1:
        raise ValueError("identifiers resolve to different papers")
    return next(iter(paper_ids), None)


async def ensure_paper_identifiers(
    session: AsyncSession,
    *,
    paper_id: uuid.UUID,
    identifiers: list[Identifier],
    source: str,
    verified: bool = False,
) -> list[PaperIdentifier]:
    rows: list[PaperIdentifier] = []
    paper = await session.get(Paper, paper_id)
    for raw_identifier in identifiers:
        identifier = normalize_identifier(raw_identifier.namespace, raw_identifier.value)
        existing = (
            await session.execute(
                select(PaperIdentifier).where(
                    PaperIdentifier.namespace == identifier.namespace,
                    PaperIdentifier.normalized_value == identifier.value,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.paper_id != paper_id:
                raise ValueError(
                    f"{identifier.namespace}:{identifier.value} belongs to another paper"
                )
            rows.append(existing)
            continue
        row = PaperIdentifier(
            paper_id=paper_id,
            namespace=identifier.namespace,
            raw_value=raw_identifier.value,
            normalized_value=identifier.value,
            source=source,
            confidence=1.0,
            is_verified=verified,
        )
        session.add(row)
        if paper is not None and "identifiers" in paper.__dict__:
            paper.identifiers.append(row)
        rows.append(row)
    if rows:
        await session.flush()
    return rows


async def ensure_record_identifiers(
    session: AsyncSession, *, paper_id: uuid.UUID, record: ProviderRecord
) -> list[PaperIdentifier]:
    return await ensure_paper_identifiers(
        session,
        paper_id=paper_id,
        identifiers=list(record.identifiers),
        source=record.source,
        verified=True,
    )
