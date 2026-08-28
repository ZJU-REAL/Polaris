"""Reproducible query generation, budget allocation, fusion, and model reranking."""

from __future__ import annotations

import json
import logging
import re
import shlex
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.core.llm.base import Message
from app.services.literature.discovery_ranking import (
    RankedCandidate,
    candidate_identity,
    merge_candidates,
    normalized_score_weights,
)

logger = logging.getLogger(__name__)

QUERY_PLAN_VERSION = "literature-query-v2"
RANKING_VERSION = "literature-ranking-v2"
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_LATIN_RE = re.compile(r"[A-Za-z]")
_NON_LATIN_LETTER_RE = re.compile(r"[^\W\d_A-Za-z]", re.UNICODE)
_PLAIN_QUERY_SOURCES = frozenset({"openalex", "semantic", "crossref"})


class QueryGenerationError(RuntimeError):
    """No validated scholarly query can be produced without inventing broad terms."""


@dataclass(frozen=True, slots=True)
class QueryFamily:
    purpose: Literal["core", "coverage"]
    query: str
    seed_id: str | None = None
    discipline: str | None = None
    role: str | None = None


@dataclass(frozen=True, slots=True)
class ExecutableQuery:
    source: str
    purpose: Literal["core", "coverage"]
    query: str
    limit: int
    seed_id: str | None = None
    discipline: str | None = None
    role: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "purpose": self.purpose,
            "query": self.query,
            "limit": self.limit,
            **({"seed_id": self.seed_id} if self.seed_id else {}),
            **({"discipline": self.discipline} if self.discipline else {}),
            **({"role": self.role} if self.role else {}),
        }


def _clean(value: Any, *, limit: int = 4000) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _extract_json(value: str) -> Any:
    text = value.strip()
    fenced = _JSON_FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    first = min((index for index in (text.find("{"), text.find("[")) if index >= 0), default=-1)
    if first > 0:
        text = text[first:]
    decoder = json.JSONDecoder()
    payload, _ = decoder.raw_decode(text)
    return payload


def _is_english_query(query: str) -> bool:
    latin = len(_LATIN_RE.findall(query))
    other = len(_NON_LATIN_LETTER_RE.findall(query))
    return latin >= 4 and latin / max(1, latin + other) >= 0.85


def _english_fallback_families(
    *, topic: str, keywords: Sequence[str], query_plan: Mapping[str, Any] | None
) -> list[QueryFamily]:
    """Build a bounded English fallback when the query model is unavailable."""

    seeds = _seed_families(topic=topic, keywords=keywords, query_plan=query_plan)
    latin_terms: list[str] = []
    for value in (topic, *keywords, *(seed.query for seed in seeds)):
        terms = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", str(value))
        latin_terms.extend(terms)
    latin_terms = list(dict.fromkeys(term.casefold() for term in latin_terms))[:8]
    if len(latin_terms) < 2:
        raise QueryGenerationError("QUERY_GENERATION_FAILED")
    core = " AND ".join(f'"{term}"' for term in latin_terms[:4])
    output = [QueryFamily(purpose="core", query=core, seed_id="fallback-core")]
    if len(latin_terms) > 4:
        output.append(
            QueryFamily(
                purpose="coverage",
                query=" OR ".join(f'"{term}"' for term in latin_terms[4:]),
                seed_id="fallback-coverage",
            )
        )
    return output


def compile_source_query(source: str, query: str) -> str:
    """Compile the generated Boolean expression for a provider's query contract."""

    normalized_source = source.strip().lower()
    cleaned = _clean(query, limit=800)
    if normalized_source not in _PLAIN_QUERY_SOURCES:
        return cleaned
    try:
        tokens = shlex.split(cleaned.replace("(", " ").replace(")", " "))
    except ValueError:
        tokens = cleaned.replace('"', " ").split()
    output: list[str] = []
    skip_next = False
    for token in tokens:
        upper = token.upper()
        if upper == "NOT":
            skip_next = True
            continue
        if upper in {"AND", "OR"}:
            continue
        if skip_next or token.startswith("-"):
            skip_next = False
            continue
        value = token.strip()
        if value and value.casefold() not in {item.casefold() for item in output}:
            output.append(value)
    return " ".join(output) or cleaned


def _seed_families(
    *, topic: str, keywords: Sequence[str], query_plan: Mapping[str, Any] | None
) -> list[QueryFamily]:
    rows = query_plan.get("queries") if isinstance(query_plan, Mapping) else None
    output: list[QueryFamily] = []
    seen: set[tuple[str, str | None]] = set()
    if isinstance(rows, list):
        for index, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            query = _clean(row.get("query"))
            purpose = str(row.get("purpose") or "coverage").lower()
            purpose = purpose if purpose in {"core", "coverage"} else "coverage"
            seed_id = _clean(row.get("id") or row.get("seed_id"), limit=64) or f"seed-{index + 1}"
            key = (query.casefold(), seed_id)
            if not query or key in seen:
                continue
            seen.add(key)
            output.append(
                QueryFamily(
                    purpose=purpose,
                    query=query,
                    seed_id=seed_id,
                    discipline=_clean(row.get("discipline"), limit=255) or None,
                    role=_clean(row.get("role"), limit=32) or None,
                )
            )
    if output:
        return output[:12]

    explicit_query = _clean(query_plan.get("query")) if isinstance(query_plan, Mapping) else ""
    topic = explicit_query or _clean(topic)
    terms = list(dict.fromkeys(_clean(item, limit=160) for item in keywords if _clean(item)))[:12]
    output.append(QueryFamily(purpose="core", query=topic, seed_id="core"))
    if terms:
        output.append(QueryFamily(purpose="coverage", query=" OR ".join(terms), seed_id="coverage"))
    return output


def _validated_generated_families(payload: Any, seeds: Sequence[QueryFamily]) -> list[QueryFamily]:
    rows = payload.get("queries") if isinstance(payload, Mapping) else None
    if not isinstance(rows, list):
        raise ValueError("queries must be a list")
    seed_by_id = {seed.seed_id: seed for seed in seeds if seed.seed_id}
    output: list[QueryFamily] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        query = _clean(row.get("query"))
        if not query or len(query) > 800 or not _is_english_query(query):
            continue
        purpose = str(row.get("purpose") or "coverage").lower()
        if purpose not in {"core", "coverage"}:
            continue
        seed_id = _clean(row.get("seed_id"), limit=64) or None
        seed = seed_by_id.get(seed_id)
        key = query.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append(
            QueryFamily(
                purpose=purpose,
                query=query,
                seed_id=seed_id,
                discipline=seed.discipline if seed else None,
                role=seed.role if seed else None,
            )
        )
    required_purposes = {item.purpose for item in seeds}
    returned_purposes = {item.purpose for item in output}
    if not output or not required_purposes.issubset(returned_purposes):
        missing = ", ".join(sorted(required_purposes - returned_purposes))
        raise ValueError(f"valid English query purposes missing: {missing or 'core'}")
    return output[:12]


async def generate_query_families(
    *,
    llm_router: Any,
    topic: str,
    keywords: Sequence[str],
    excluded_keywords: Sequence[str],
    query_plan: Mapping[str, Any] | None,
    user_id: uuid.UUID | None,
    library_id: uuid.UUID,
    score_rubric: Any = None,
    max_attempts: int = 3,
) -> tuple[list[QueryFamily], dict[str, Any]]:
    """Generate validated English query families and return an auditable snapshot."""

    seeds = _seed_families(topic=topic, keywords=keywords, query_plan=query_plan)
    seed_payload = [
        {
            "seed_id": item.seed_id,
            "purpose": item.purpose,
            "query": item.query,
            "discipline": item.discipline,
            "role": item.role,
        }
        for item in seeds
    ]
    prompt = {
        "topic": topic,
        "keywords": list(keywords),
        "excluded_keywords": list(excluded_keywords),
        "score_rubric": score_rubric,
        "seed_queries": seed_payload,
    }
    errors: list[str] = []
    for attempt in range(1, max_attempts + 1):
        try:
            retry_context = {"previous_validation_errors": errors[-2:]} if errors else {}
            result = await llm_router.complete(
                "extract",
                [
                    Message(
                        role="system",
                        content=(
                            "Create high-precision scholarly search queries. "
                            "Return strict JSON only: "
                            '{"queries":[{"seed_id":"...","purpose":"core|coverage",'
                            '"query":"English Boolean query"}]}. '
                            "Use English technical terms, preserve seed_id when a seed is refined, "
                            "include at least one core query, avoid broad "
                            "single-word queries, and do not include excluded concepts."
                        ),
                    ),
                    Message(
                        role="user",
                        content=json.dumps({**prompt, **retry_context}, ensure_ascii=False),
                    ),
                ],
                temperature=0.1,
                max_tokens=1800,
                user_id=user_id,
                library_id=library_id,
            )
            families = _validated_generated_families(_extract_json(result.content), seeds)
            return families, {
                "version": QUERY_PLAN_VERSION,
                "mode": "model",
                "model": result.model,
                "attempts": attempt,
                "validation_errors": errors,
            }
        except Exception as exc:  # noqa: BLE001 - deterministic fallback is required
            errors.append(f"{type(exc).__name__}: {exc}"[:500])
            logger.debug("literature query generation attempt %s failed", attempt, exc_info=True)
    logger.warning("literature query generation failed; using deterministic fallback")
    fallback = _english_fallback_families(topic=topic, keywords=keywords, query_plan=query_plan)
    return fallback, {
        "version": QUERY_PLAN_VERSION,
        "mode": "deterministic_fallback",
        "model": None,
        "attempts": max_attempts,
        "validation_errors": errors,
    }


def allocate_query_budget(
    *, sources: Sequence[str], families: Sequence[QueryFamily], candidate_budget: int
) -> list[ExecutableQuery]:
    """Allocate one aggregate budget across source-query pairs, core pairs first."""

    if candidate_budget < 1:
        raise ValueError("candidate_budget must be positive")
    unique_sources = list(dict.fromkeys(_clean(source, limit=64).lower() for source in sources))
    unique_sources = [source for source in unique_sources if source]
    ordered_families = sorted(families, key=lambda item: item.purpose != "core")
    pairs = [(source, family) for family in ordered_families for source in unique_sources]
    active = pairs[:candidate_budget]
    if not active:
        return []
    quotas = [1] * len(active)
    remaining = candidate_budget - len(active)
    for index in range(remaining):
        quotas[index % len(quotas)] += 1
    return [
        ExecutableQuery(
            source=source,
            purpose=family.purpose,
            query=compile_source_query(source, family.query),
            limit=quota,
            seed_id=family.seed_id,
            discipline=family.discipline,
            role=family.role,
        )
        for (source, family), quota in zip(active, quotas, strict=True)
    ]


def add_retrieval_hit(
    candidate: Mapping[str, Any], *, task: ExecutableQuery, rank: int
) -> dict[str, Any]:
    output = dict(candidate)
    metadata = dict(output.get("metadata") or {})
    metadata["retrieval_hits"] = [
        {
            "source": task.source,
            "purpose": task.purpose,
            "query": task.query,
            "rank": rank,
            **({"seed_id": task.seed_id} if task.seed_id else {}),
            **({"discipline": task.discipline} if task.discipline else {}),
            **({"role": task.role} if task.role else {}),
        }
    ]
    output["metadata"] = metadata
    return output


def fuse_candidates(
    candidates: Sequence[Mapping[str, Any]], *, executed_query_count: int
) -> list[dict[str, Any]]:
    """Merge duplicates and attach a normalized reciprocal-rank-fusion score."""

    merged = merge_candidates(candidates)
    denominator = max(1, executed_query_count) / 61
    for candidate in merged:
        metadata = candidate.get("metadata")
        hits = metadata.get("retrieval_hits") if isinstance(metadata, Mapping) else []
        score = sum(1 / (60 + max(1, int(hit.get("rank") or 1))) for hit in hits or [])
        candidate["retrieval_score"] = round(min(1.0, score / denominator), 6)
    return merged


def _rerank_document(item: RankedCandidate) -> str:
    candidate = item.candidate
    return "\n".join(
        part
        for part in (
            _clean(candidate.get("title"), limit=2000),
            _clean(candidate.get("abstract"), limit=8000),
            _clean(candidate.get("keywords"), limit=1000),
        )
        if part
    )[:10_000]


def _tier(score: float) -> Literal["core", "supporting", "exploratory"]:
    if score >= 0.75:
        return "core"
    if score >= 0.50:
        return "supporting"
    return "exploratory"


async def model_rerank(
    *,
    llm_router: Any,
    topic: str,
    ranked: Sequence[RankedCandidate],
    weights: Mapping[str, float] | None,
    limit: int,
    user_id: uuid.UUID | None,
    library_id: uuid.UUID,
    score_rubric: Any = None,
) -> tuple[list[RankedCandidate], dict[str, Any]]:
    """Replace only relevance with model scores; retain deterministic quality dimensions."""

    pool = list(ranked)
    if not pool or limit <= 0:
        return [], {"version": RANKING_VERSION, "mode": "deterministic", "model": None}
    try:
        rerank_query = topic
        if score_rubric:
            rerank_query = (
                f"{topic}\nLibrary relevance rubric: "
                f"{json.dumps(score_rubric, ensure_ascii=False, default=str)}"
            )[:8000]
        results = await llm_router.rerank(
            rerank_query,
            [_rerank_document(item) for item in pool],
            top_n=len(pool),
            user_id=user_id,
            library_id=library_id,
        )
        normalized_weights = normalized_score_weights(weights)
        model_scores = {
            index: max(0.0, min(1.0, float(model_score)))
            for index, model_score in results
            if 0 <= index < len(pool)
        }
        if not model_scores:
            raise ValueError("reranker returned no valid rows")
        output: list[RankedCandidate] = []
        for index, item in enumerate(pool):
            relevance = model_scores.get(index, item.dimensions["relevance"])
            dimensions = {**item.dimensions, "relevance": round(relevance, 6)}
            score = round(
                sum(dimensions[name] * normalized_weights[name] for name in normalized_weights),
                6,
            )
            output.append(
                RankedCandidate(
                    identity=item.identity,
                    candidate=item.candidate,
                    score=score,
                    tier=_tier(score),
                    dimensions=dimensions,
                    reasons=(
                        *item.reasons,
                        f"model_relevance={relevance:.3f}"
                        if index in model_scores
                        else "model_relevance=not_returned; deterministic score retained",
                    ),
                )
            )
        output.sort(key=lambda item: (-item.score, item.identity))
        try:
            model = await llm_router.model_name("rerank", user_id)
        except Exception:  # model metadata is advisory; a successful rerank is still usable
            model = None
        return output[:limit], {
            "version": RANKING_VERSION,
            "mode": "model",
            "model": model,
            "pool_size": len(pool),
            "rubric_applied": bool(score_rubric),
        }
    except Exception as exc:  # noqa: BLE001 - deterministic fallback is required
        logger.warning("literature rerank failed, using deterministic ranking", exc_info=True)
        return pool[:limit], {
            "version": RANKING_VERSION,
            "mode": "deterministic_fallback",
            "model": None,
            "pool_size": len(pool),
            "rubric_applied": bool(score_rubric),
            "error": f"{type(exc).__name__}: {exc}"[:500],
        }


def retrieval_identity(candidate: Mapping[str, Any]) -> str:
    """Expose the stable identity for diagnostics without duplicating normalization."""

    return candidate_identity(candidate)
