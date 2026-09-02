"""句子/段落级证据锚点的生成、持久化和内容版本回退。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import exists, select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.evidence import PaperEvidenceAnchor
from app.models.library_direction import LibraryPaper
from app.models.paper_assets import AssetGrant
from app.models.paper_content import PaperContentChunk, PaperContentVersion
from app.schemas.evidence import EvidenceResolution

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?。！？])(?:[\"'”’»\)\]]+)?\s+|\n+")
_JOINED_HYPHEN = "\ufff0"
_WORD_RE = re.compile(r"[\w\u4e00-\u9fff]+")
_READY_CONTENT_STATUSES = ("ready", "ready_fallback", "vector_ready")


def normalize_evidence_text(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).replace("\u00ad", "")
    text = re.sub(r"-\s*\n\s*", "", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def content_revision(text: str) -> str:
    return hashlib.sha256(normalize_evidence_text(text).encode("utf-8")).hexdigest()


def split_paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n\s*\n+", text) if part.strip()]


def split_sentences(text: str) -> list[str]:
    protected = re.sub(r"-\s*\n\s*", f"-{_JOINED_HYPHEN}", text)
    sentences = [
        part.replace(_JOINED_HYPHEN, "\n").strip()
        for part in _SENTENCE_BOUNDARY.split(protected)
        if part.strip()
    ]
    return sentences or ([text.strip()] if text.strip() else [])


@dataclass(frozen=True, slots=True)
class AnchorPayload:
    paper_id: uuid.UUID
    chunk_id: uuid.UUID | None
    source: str
    content_revision: str
    anchor_key: str
    anchor_type: str
    seq: int | None
    paragraph_index: int | None
    sentence_index: int | None
    quoted_text: str
    normalized_text: str
    locator: dict[str, Any]


def _locator(
    *,
    page_start: int | None,
    page_end: int | None,
    rects: list[dict[str, float]] | None,
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    if page_start is not None:
        value["page_start"] = page_start
    if page_end is not None:
        value["page_end"] = page_end
    if rects:
        value["rects"] = rects
    return value


def build_chunk_anchor_payloads(
    *,
    paper_id: uuid.UUID,
    chunk_id: uuid.UUID | None,
    seq: int | None,
    text: str,
    source: str,
    page_start: int | None = None,
    page_end: int | None = None,
    rects: list[dict[str, float]] | None = None,
) -> list[AnchorPayload]:
    raw = text.strip()
    if not raw:
        return []
    revision = content_revision(raw)
    common = {
        "paper_id": paper_id,
        "chunk_id": chunk_id,
        "source": source,
        "content_revision": revision,
        "seq": seq,
    }
    base_locator = _locator(page_start=page_start, page_end=page_end, rects=rects)
    payloads = [
        AnchorPayload(
            **common,
            anchor_key=f"chunk:{chunk_id or 'na'}:{seq if seq is not None else 'na'}",
            anchor_type="chunk",
            paragraph_index=None,
            sentence_index=None,
            quoted_text=raw,
            normalized_text=normalize_evidence_text(raw),
            locator=base_locator,
        )
    ]
    for paragraph_index, paragraph in enumerate(split_paragraphs(raw)):
        payloads.append(
            AnchorPayload(
                **common,
                anchor_key=(
                    f"paragraph:{chunk_id or 'na'}:"
                    f"{seq if seq is not None else 'na'}:{paragraph_index}"
                ),
                anchor_type="paragraph",
                paragraph_index=paragraph_index,
                sentence_index=None,
                quoted_text=paragraph,
                normalized_text=normalize_evidence_text(paragraph),
                locator=base_locator,
            )
        )
        for sentence_index, sentence in enumerate(split_sentences(paragraph)):
            payloads.append(
                AnchorPayload(
                    **common,
                    anchor_key=(
                        f"sentence:{chunk_id or 'na'}:{seq if seq is not None else 'na'}:"
                        f"{paragraph_index}:{sentence_index}"
                    ),
                    anchor_type="sentence",
                    paragraph_index=paragraph_index,
                    sentence_index=sentence_index,
                    quoted_text=sentence,
                    normalized_text=normalize_evidence_text(sentence),
                    locator=base_locator,
                )
            )
    return payloads


async def persist_chunk_anchors(
    session: AsyncSession,
    *,
    paper_id: uuid.UUID,
    chunks: Iterable[PaperContentChunk],
    source: str | None = None,
    locators: Mapping[uuid.UUID, Mapping[str, Any]] | None = None,
) -> int:
    created = 0
    for chunk in chunks:
        raw = str(chunk.text or "").strip()
        if not raw:
            continue
        locator = dict(locators.get(chunk.id, {}) if locators else {})
        locator.setdefault("page_start", chunk.page_start)
        locator.setdefault("page_end", chunk.page_end)
        if not locator.get("rects") and isinstance(chunk.rects, list):
            locator["rects"] = chunk.rects
        for payload in build_chunk_anchor_payloads(
            paper_id=paper_id,
            chunk_id=chunk.id,
            seq=chunk.seq,
            text=raw,
            source=source or "content",
            page_start=locator.get("page_start"),
            page_end=locator.get("page_end"),
            rects=locator.get("rects"),
        ):
            exists = await session.scalar(
                select(PaperEvidenceAnchor.id).where(
                    PaperEvidenceAnchor.paper_id == payload.paper_id,
                    PaperEvidenceAnchor.content_revision == payload.content_revision,
                    PaperEvidenceAnchor.anchor_key == payload.anchor_key,
                )
            )
            if exists is None:
                session.add(PaperEvidenceAnchor(**asdict(payload)))
                created += 1
    if created:
        await session.flush()
    return created


def _locator_values(
    anchor: PaperEvidenceAnchor,
) -> tuple[int | None, int | None, list[dict[str, float]]]:
    locator = anchor.locator if isinstance(anchor.locator, dict) else {}
    rects = locator.get("rects") if isinstance(locator.get("rects"), list) else []
    return locator.get("page_start"), locator.get("page_end"), rects


def _href(paper_id: uuid.UUID, anchor_id: uuid.UUID | None) -> str:
    suffix = f"&evidence={anchor_id}" if anchor_id else ""
    return f"/papers/{paper_id}/read?evidence=1{suffix}"


def _resolution(
    anchor: PaperEvidenceAnchor,
    *,
    status: str,
    anchor_type: str,
    quoted_text: str,
    chunk: PaperContentChunk | None = None,
) -> EvidenceResolution:
    if chunk is None:
        page_start, page_end, rects = _locator_values(anchor)
    else:
        page_start, page_end = chunk.page_start, chunk.page_end
        rects = chunk.rects if isinstance(chunk.rects, list) else []
    return EvidenceResolution(
        paper_id=anchor.paper_id,
        anchor_id=anchor.id,
        status=status,
        anchor_type=anchor_type,
        quoted_text=quoted_text,
        chunk_id=anchor.chunk_id,
        seq=anchor.seq,
        page_start=page_start,
        page_end=page_end,
        rects=rects,
        section_path=(chunk.section_path or []) if chunk is not None else [],
        href=_href(anchor.paper_id, anchor.id),
    )


async def resolve_evidence_anchor(
    session: AsyncSession,
    anchor: PaperEvidenceAnchor,
    *,
    current_chunks: Sequence[PaperContentChunk] | None = None,
) -> EvidenceResolution:
    chunks = list(current_chunks or [])
    if not chunks:
        chunks = list(
            (
                await session.execute(
                    select(PaperContentChunk)
                    .join(
                        PaperContentVersion,
                        PaperContentVersion.id == PaperContentChunk.content_version_id,
                    )
                    .where(
                        PaperContentVersion.paper_id == anchor.paper_id,
                        PaperContentVersion.is_current.is_(True),
                    )
                )
            )
            .scalars()
        )
    current = next((chunk for chunk in chunks if chunk.id == anchor.chunk_id), None)
    if current is not None and content_revision(current.text) == anchor.content_revision:
        return _resolution(
            anchor,
            status="exact",
            anchor_type=anchor.anchor_type,
            quoted_text=anchor.quoted_text,
            chunk=current,
        )
    needle = anchor.normalized_text
    matches: list[PaperContentChunk] = []
    for chunk in chunks:
        normalized = normalize_evidence_text(chunk.text)
        if needle and needle in normalized:
            matches.append(chunk)
    if len(matches) == 1:
        matched = matches[0]
        fallback = (
            anchor.anchor_type if anchor.anchor_type in {"sentence", "paragraph"} else "chunk"
        )
        return _resolution(
            anchor,
            status=fallback,
            anchor_type=fallback,
            quoted_text=anchor.quoted_text,
            chunk=matched,
        )
    if current is not None:
        return _resolution(
            anchor,
            status="chunk",
            anchor_type="chunk",
            quoted_text=current.text,
            chunk=current,
        )
    return EvidenceResolution(
        paper_id=anchor.paper_id,
        anchor_id=anchor.id,
        status="paper",
        anchor_type="paper",
        quoted_text=anchor.quoted_text,
        href=_href(anchor.paper_id, anchor.id),
    )


async def current_fulltext_evidence(
    session: AsyncSession,
    *,
    paper_id: uuid.UUID,
    library_ids: Sequence[uuid.UUID] | None = None,
    query: str | None = None,
    offset: int = 0,
    limit: int = 8,
) -> dict[str, Any] | None:
    """Return current parsed chunks with sentence anchors for agent and MCP tools."""
    version_query = select(PaperContentVersion).where(
            PaperContentVersion.paper_id == paper_id,
            PaperContentVersion.is_current.is_(True),
            PaperContentVersion.status.in_(_READY_CONTENT_STATUSES),
        )
    if library_ids is not None:
        if not library_ids:
            return None
        version_query = version_query.where(_asset_grant_exists(library_ids))
    version = await session.scalar(version_query)
    if version is None:
        return None
    page_size = max(1, min(limit, 20))
    chunk_query = select(PaperContentChunk).where(
        PaperContentChunk.content_version_id == version.id
    )
    if query:
        chunks = list(
            (
                await session.execute(chunk_query.order_by(PaperContentChunk.seq))
            )
            .scalars()
            .all()
        )
        terms = {part for part in normalize_evidence_text(query).split() if len(part) > 1}
        chunks.sort(
            key=lambda chunk: (
                -sum(term in normalize_evidence_text(chunk.text) for term in terms),
                chunk.seq,
            )
        )
        selected = chunks[max(0, offset) : max(0, offset) + page_size]
        has_more = max(0, offset) + len(selected) < len(chunks)
    else:
        rows = list(
            (
                await session.execute(
                    chunk_query.order_by(PaperContentChunk.seq)
                    .offset(max(0, offset))
                    .limit(page_size + 1)
                )
            )
            .scalars()
            .all()
        )
        selected = rows[:page_size]
        has_more = len(rows) > page_size
    if not selected:
        return {
            "version_id": str(version.id),
            "parser": version.parser,
            "chunks": [],
            "next_offset": None,
        }
    anchors = list(
        (
            await session.execute(
                select(PaperEvidenceAnchor)
                .where(
                    PaperEvidenceAnchor.chunk_id.in_([chunk.id for chunk in selected]),
                    PaperEvidenceAnchor.anchor_type == "sentence",
                )
                .order_by(
                    PaperEvidenceAnchor.seq,
                    PaperEvidenceAnchor.paragraph_index,
                    PaperEvidenceAnchor.sentence_index,
                )
            )
        )
        .scalars()
        .all()
    )
    by_chunk: dict[uuid.UUID, list[PaperEvidenceAnchor]] = {}
    for anchor in anchors:
        by_chunk.setdefault(anchor.chunk_id, []).append(anchor)
    rows: list[dict[str, Any]] = []
    citation_no = 0
    for chunk in selected:
        references = []
        for anchor in by_chunk.get(chunk.id, []):
            citation_no += 1
            page_start, page_end, rects = _locator_values(anchor)
            references.append(
                {
                    "citation_no": citation_no,
                    "anchor_id": str(anchor.id),
                    "quoted_text": anchor.quoted_text,
                    "page_start": page_start,
                    "page_end": page_end,
                    "rects": rects,
                    "href": _href(paper_id, anchor.id),
                }
            )
        rows.append(
            {
                "chunk_id": str(chunk.id),
                "seq": chunk.seq,
                "text": chunk.text,
                "page_start": chunk.page_start,
                "page_end": chunk.page_end,
                "section_path": chunk.section_path or [],
                "evidence": references,
            }
        )
    next_offset = max(0, offset) + len(selected) if has_more else None
    return {
        "version_id": str(version.id),
        "parser": version.parser,
        "chunks": rows,
        "next_offset": next_offset,
    }


@dataclass(frozen=True, slots=True)
class FulltextChunkHit:
    chunk: PaperContentChunk
    paper_id: uuid.UUID
    score: float


def fulltext_vector_search_supported(session: AsyncSession) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def _library_scope_exists(library_ids: Sequence[uuid.UUID]):
    from app.services.papers import PAPER_STATUS_GROUPS

    return exists(
        select(LibraryPaper.paper_id).where(
            LibraryPaper.paper_id == PaperContentVersion.paper_id,
            LibraryPaper.library_id.in_(library_ids),
            LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"]),
        )
    )


def _asset_grant_exists(library_ids: Sequence[uuid.UUID]):
    return exists(
        select(AssetGrant.id).where(
            AssetGrant.asset_id == PaperContentVersion.asset_id,
            AssetGrant.library_id.in_(library_ids),
            AssetGrant.status == "active",
            AssetGrant.can_read.is_(True),
        )
    )


async def keyword_search_current_fulltext(
    session: AsyncSession,
    *,
    library_ids: Sequence[uuid.UUID],
    query: str,
    limit: int,
) -> list[FulltextChunkHit]:
    """Search current parsed content without selecting JSON columns through DISTINCT."""
    if not library_ids:
        return []
    terms = [term for term in _WORD_RE.findall(query.casefold()) if len(term) >= 2][:8]
    if not terms:
        return []
    condition = PaperContentChunk.text.ilike(f"%{terms[0]}%")
    for term in terms[1:]:
        condition |= PaperContentChunk.text.ilike(f"%{term}%")
    rows = (
        await session.execute(
            select(PaperContentChunk, PaperContentVersion.paper_id)
            .join(
                PaperContentVersion,
                PaperContentVersion.id == PaperContentChunk.content_version_id,
            )
            .where(
                PaperContentVersion.is_current.is_(True),
                PaperContentVersion.status.in_(_READY_CONTENT_STATUSES),
                _library_scope_exists(library_ids),
                _asset_grant_exists(library_ids),
                condition,
            )
            .limit(max(1, limit) * 5)
        )
    ).all()

    def score_of(chunk: PaperContentChunk) -> float:
        lowered = chunk.text.casefold()
        return float(sum(term in lowered for term in terms))

    hits = [
        FulltextChunkHit(chunk=chunk, paper_id=paper_id, score=score_of(chunk))
        for chunk, paper_id in rows
    ]
    return sorted(hits, key=lambda hit: (-hit.score, hit.chunk.seq))[:limit]


async def semantic_search_current_fulltext(
    session: AsyncSession,
    *,
    library_ids: Sequence[uuid.UUID],
    query_vector: list[float],
    space_key: str,
    limit: int,
) -> list[FulltextChunkHit]:
    """Search current full-text vectors with permission checks in correlated EXISTS."""
    if not library_ids or not fulltext_vector_search_supported(session):
        return []
    from app.services.papers import PAPER_STATUS_GROUPS

    rows = (
        await session.execute(
            sa_text(
                "SELECT c.id, cv.paper_id, "
                "1 - (v.embedding <=> CAST(:qv AS vector)) AS score "
                "FROM paper_content_chunk_vectors v "
                "JOIN paper_content_chunks c ON c.id = v.chunk_id "
                "JOIN paper_content_versions cv ON cv.id = c.content_version_id "
                "WHERE v.space = :space AND cv.is_current = true "
                "AND cv.status = ANY(CAST(:content_statuses AS varchar[])) "
                "AND EXISTS (SELECT 1 FROM library_papers lp "
                "WHERE lp.paper_id = cv.paper_id "
                "AND lp.library_id = ANY(CAST(:libs AS uuid[])) "
                "AND lp.status = ANY(CAST(:paper_statuses AS varchar[]))) "
                "AND EXISTS (SELECT 1 FROM asset_grants ag "
                "WHERE ag.asset_id = cv.asset_id "
                "AND ag.library_id = ANY(CAST(:libs AS uuid[])) "
                "AND ag.status = 'active' AND ag.can_read = true) "
                "ORDER BY score DESC LIMIT :k"
            ),
            {
                "qv": json.dumps(query_vector),
                "space": space_key,
                "libs": [str(value) for value in library_ids],
                "content_statuses": list(_READY_CONTENT_STATUSES),
                "paper_statuses": list(PAPER_STATUS_GROUPS["library"]),
                "k": max(1, limit),
            },
        )
    ).all()
    if not rows:
        return []
    scores = {row.id: float(row.score) for row in rows}
    paper_ids = {row.id: row.paper_id for row in rows}
    chunks = list(
        (
            await session.execute(
                select(PaperContentChunk).where(PaperContentChunk.id.in_(scores))
            )
        )
        .scalars()
        .all()
    )
    by_id = {chunk.id: chunk for chunk in chunks}
    return [
        FulltextChunkHit(
            chunk=by_id[row.id],
            paper_id=paper_ids[row.id],
            score=scores[row.id],
        )
        for row in rows
        if row.id in by_id
    ]


async def sentence_evidence_for_chunks(
    session: AsyncSession,
    chunks: Sequence[PaperContentChunk],
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    if not chunks:
        return {}
    anchors = list(
        (
            await session.execute(
                select(PaperEvidenceAnchor).where(
                    PaperEvidenceAnchor.chunk_id.in_([chunk.id for chunk in chunks]),
                    PaperEvidenceAnchor.anchor_type == "sentence",
                )
            )
        )
        .scalars()
        .all()
    )
    result: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for anchor in anchors:
        page_start, page_end, rects = _locator_values(anchor)
        result.setdefault(anchor.chunk_id, []).append(
            {
                "anchor_id": str(anchor.id),
                "quoted_text": anchor.quoted_text,
                "page_start": page_start,
                "page_end": page_end,
                "rects": rects,
                "href": _href(anchor.paper_id, anchor.id),
            }
        )
    return result
