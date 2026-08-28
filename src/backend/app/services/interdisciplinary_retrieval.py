"""Compile confirmed interdisciplinary profiles into auditable discovery plans."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.interdisciplinary import InterdisciplinaryResearchProfile
from app.models.library_direction import DirectionLibrary
from app.services.literature.discovery_ranking import RankedCandidate

_CHANNEL_ROLES = {"primary", "related", "bridge", "method_transfer"}


def _clean(value: object, *, limit: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def build_query_matrix(
    *, topic: str, primary_domain: str, related_domains: Sequence[str], keywords: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Build deterministic discipline and bridge channels from a confirmed scope."""
    topic = _clean(topic)
    primary = _clean(primary_domain, limit=255)
    related = list(
        dict.fromkeys(_clean(item, limit=255) for item in related_domains if _clean(item))
    )
    terms = list(dict.fromkeys(_clean(item, limit=120) for item in keywords if _clean(item)))[:8]
    suffix = " OR ".join(f'"{term}"' if " " in term else term for term in terms)

    def query(*parts: str) -> str:
        base = " AND ".join(f'"{part}"' if " " in part else part for part in parts if part)
        return f"({base}) AND ({suffix})" if suffix else base

    channels: list[dict[str, Any]] = [
        {"id": "primary", "discipline": primary, "role": "primary", "query": query(topic, primary)}
    ]
    for index, domain in enumerate(related, start=1):
        channels.extend(
            (
                {
                    "id": f"related-{index}",
                    "discipline": domain,
                    "role": "related",
                    "query": query(topic, domain),
                },
                {
                    "id": f"bridge-{index}",
                    "discipline": f"{primary} + {domain}",
                    "role": "bridge",
                    "query": query(topic, primary, domain),
                },
            )
        )
    return channels[:24]


def normalize_query_matrix(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate editable channel rows without accepting opaque provider syntax."""
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        role = _clean(row.get("role"), limit=32).lower()
        query = _clean(row.get("query"), limit=4000)
        discipline = _clean(row.get("discipline"), limit=255)
        if role not in _CHANNEL_ROLES or not query or not discipline:
            continue
        normalized.append(
            {
                "id": _clean(row.get("id"), limit=64) or f"channel-{index + 1}",
                "discipline": discipline,
                "role": role,
                "query": query,
            }
        )
    return normalized[:24]


async def apply_profile_to_query_plan(
    session: AsyncSession,
    *,
    library: DirectionLibrary,
    topic: str,
    query_plan: dict[str, Any] | None,
    source_config: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a run snapshot for a confirmed dedicated library, otherwise preserve input."""
    current = dict(query_plan or {})
    if library.library_kind != "interdisciplinary" or library.interdisciplinary_project_id is None:
        return query_plan
    profile = await session.scalar(
        select(InterdisciplinaryResearchProfile).where(
            InterdisciplinaryResearchProfile.project_id == library.interdisciplinary_project_id,
            InterdisciplinaryResearchProfile.status == "confirmed",
        )
    )
    if profile is None:
        return query_plan
    config = source_config if isinstance(source_config, dict) else {}
    keywords = [str(item) for item in config.get("keywords") or []]
    channels = normalize_query_matrix(profile.query_matrix or []) or build_query_matrix(
        topic=topic,
        primary_domain=profile.primary_domain,
        related_domains=profile.related_domains,
        keywords=keywords,
    )
    sources = [
        str(item).strip().lower()
        for item in config.get("sources") or []
        if str(item).strip()
    ]
    current["queries"] = [
        {**channel, "source": source}
        for source in dict.fromkeys(sources)
        for channel in channels
    ]
    current["interdisciplinary"] = {
        "profile_id": str(profile.id),
        "profile_version": profile.version,
        "primary_domain": profile.primary_domain,
        "related_domains": list(profile.related_domains),
        "evidence_balance": profile.evidence_balance or {},
        "channels": channels,
    }
    return current


def rerank_interdisciplinary(
    ranked: Sequence[RankedCandidate], *, query_plan: dict[str, Any] | None, limit: int
) -> list[RankedCandidate]:
    """Reward evidenced discipline bridges while retaining base literature quality."""
    config = (query_plan or {}).get("interdisciplinary") if isinstance(query_plan, dict) else None
    if not isinstance(config, dict):
        return list(ranked)[:limit]
    output: list[RankedCandidate] = []
    for item in ranked:
        metadata = item.candidate.get("metadata")
        hits = item.candidate.get("retrieval_hits")
        if not isinstance(hits, list):
            hits = metadata.get("retrieval_hits") if isinstance(metadata, dict) else []
        hits = [hit for hit in hits or [] if isinstance(hit, dict)]
        disciplines = {str(hit.get("discipline")) for hit in hits if hit.get("discipline")}
        roles = {str(hit.get("role")) for hit in hits if hit.get("role")}
        bridge = bool(roles & {"bridge", "method_transfer"}) or len(disciplines) >= 2
        bridge_score = 1.0 if bridge else min(1.0, len(disciplines) / 2)
        dimensions = {**item.dimensions, "interdisciplinary_bridge": bridge_score}
        score = round(item.score * 0.85 + bridge_score * 0.15, 6)
        reason = "cross-discipline bridge evidence" if bridge else "single-discipline support"
        reasons = (*item.reasons, reason)
        tier = "core" if bridge and item.tier != "exploratory" else item.tier
        output.append(
            RankedCandidate(
                identity=item.identity,
                candidate=item.candidate,
                score=score,
                tier=tier,
                dimensions=dimensions,
                reasons=reasons,
            )
        )
    output.sort(key=lambda item: (-item.score, item.identity))
    return output[:limit]
