"""文献发现的确定性查询计划、候选合并和可解释排序。

本模块不访问网络、不依赖 LLM，也不写数据库。来源适配器只需把字段映射为普通
``dict``，即可复用同一套规划与排序逻辑；后续异步运行层负责持久化这些快照。
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

DEFAULT_SCORE_WEIGHTS: dict[str, float] = {
    "relevance": 0.50,
    "evidence": 0.20,
    "impact": 0.15,
    "novelty": 0.10,
    "open_access": 0.05,
}

_TOKEN_RE = re.compile(r"[a-z0-9\u3400-\u9fff]+", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SourceCapability:
    """来源查询编译能力；具体适配器显式声明，避免猜测语法。"""

    name: str
    boolean_operators: bool = True
    quoted_phrases: bool = True
    year_filter: bool = True


@dataclass(frozen=True, slots=True)
class PlannedSourceQuery:
    source: str
    purpose: Literal["core", "coverage"]
    query: str
    start_year: int | None
    end_year: int | None
    limit: int


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    identity: str
    candidate: dict[str, Any]
    score: float
    tier: Literal["core", "supporting", "exploratory"]
    dimensions: dict[str, float]
    reasons: tuple[str, ...]


def _normalized_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return re.sub(r"\s+", " ", text)


def _normalized_identifier(value: Any, *, doi: bool = False) -> str:
    text = _normalized_text(value)
    if doi:
        text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
        text = text.removeprefix("doi:")
    return text.rstrip(".").strip()


def _authors(candidate: Mapping[str, Any]) -> list[Any]:
    value = candidate.get("authors")
    return list(value) if isinstance(value, list) else []


def _first_author(candidate: Mapping[str, Any]) -> str:
    authors = _authors(candidate)
    if not authors:
        return ""
    first = authors[0]
    if isinstance(first, Mapping):
        return _normalized_text(first.get("name") or first.get("family"))
    return _normalized_text(first)


def candidate_identity(candidate: Mapping[str, Any]) -> str:
    """以稳定外部标识优先，最后退回标题、年份和首作者指纹。"""

    for prefix, names, is_doi in (
        ("doi", ("doi", "DOI"), True),
        ("pmid", ("pmid", "PMID"), False),
        ("arxiv", ("arxiv_id", "arxivId"), False),
        ("s2", ("semantic_scholar_id", "paperId"), False),
    ):
        for name in names:
            value = _normalized_identifier(candidate.get(name), doi=is_doi)
            if value:
                return f"{prefix}:{value}"

    title = _normalized_text(candidate.get("title"))
    if not title:
        raise ValueError("candidate title or stable identifier is required")
    fingerprint = "|".join((title, str(candidate.get("year") or ""), _first_author(candidate)))
    return f"title:{hashlib.sha256(fingerprint.encode('utf-8')).hexdigest()}"


def _unique_terms(values: Sequence[str], *, limit: int | None = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = " ".join(str(value).strip().split())
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        out.append(term)
        if limit is not None and len(out) >= limit:
            break
    return out


def _quote(term: str, capability: SourceCapability) -> str:
    if capability.quoted_phrases and " " in term and not term.startswith('"'):
        return f'"{term}"'
    return term


def build_query_plan(
    *,
    topic: str,
    keywords: Sequence[str],
    excluded_keywords: Sequence[str] = (),
    sources: Sequence[SourceCapability],
    start_year: int | None = None,
    end_year: int | None = None,
    per_source_limit: int = 50,
) -> list[PlannedSourceQuery]:
    """为每个来源生成核心与覆盖查询，年份始终作为结构化参数传递。"""

    topic = " ".join(topic.strip().split())
    if not topic:
        raise ValueError("topic is required")
    if start_year is not None and end_year is not None and start_year > end_year:
        raise ValueError("start_year must not be greater than end_year")
    if per_source_limit < 1:
        raise ValueError("per_source_limit must be positive")

    terms = _unique_terms(keywords, limit=12)
    exclusions = _unique_terms(excluded_keywords, limit=12)
    families: list[tuple[Literal["core", "coverage"], list[str]]] = [("core", [topic])]
    if terms:
        families.append(("coverage", terms))

    plan: list[PlannedSourceQuery] = []
    seen_sources: set[str] = set()
    for capability in sources:
        source = capability.name.strip().lower()
        if not source or source in seen_sources:
            continue
        seen_sources.add(source)
        for purpose, family_terms in families:
            quoted = [_quote(term, capability) for term in family_terms]
            if capability.boolean_operators:
                query = " OR ".join(quoted)
                if exclusions:
                    query += " " + " ".join(
                        f"NOT {_quote(term, capability)}" for term in exclusions
                    )
            else:
                query = " ".join(quoted)
            plan.append(
                PlannedSourceQuery(
                    source=source,
                    purpose=purpose,
                    query=query,
                    start_year=start_year if capability.year_filter else None,
                    end_year=end_year if capability.year_filter else None,
                    limit=per_source_limit,
                )
            )
    return plan


def _metadata_richness(candidate: Mapping[str, Any]) -> tuple[int, int]:
    present = sum(
        bool(candidate.get(field))
        for field in ("abstract", "authors", "year", "venue", "doi", "url", "pdf_url")
    )
    return present, len(str(candidate.get("abstract") or ""))


def _merge_list(left: Any, right: Any) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    values = [
        *(left if isinstance(left, list) else []),
        *(right if isinstance(right, list) else []),
    ]
    for value in values:
        key = repr(value)
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _merge_pair(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    richer, other = (
        (left, right)
        if _metadata_richness(left) >= _metadata_richness(right)
        else (right, left)
    )
    merged = dict(richer)
    for key, value in other.items():
        if key in {"sources", "retrieval_hits"}:
            merged[key] = _merge_list(merged.get(key), value)
        elif key == "citation_count":
            merged[key] = max(int(merged.get(key) or 0), int(value or 0))
        elif not merged.get(key) and value not in (None, "", []):
            merged[key] = value
    sources = _unique_terms(
        [
            *[str(v) for v in merged.get("sources") or []],
            str(left.get("source") or ""),
            str(right.get("source") or ""),
        ]
    )
    merged["sources"] = sorted(source.lower() for source in sources)
    return merged


def merge_candidates(candidates: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """合并跨来源重复项，保留更丰富的元数据和全部来源证据。"""

    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for candidate in candidates:
        identity = candidate_identity(candidate)
        if identity not in merged:
            merged[identity] = dict(candidate)
            merged[identity]["sources"] = sorted(
                source.lower()
                for source in _unique_terms(
                    [
                        *[str(v) for v in candidate.get("sources") or []],
                        str(candidate.get("source") or ""),
                    ]
                )
            )
            order.append(identity)
        else:
            merged[identity] = _merge_pair(merged[identity], candidate)
    return [merged[identity] for identity in order]


def _tokens(value: Any) -> set[str]:
    return {token.casefold() for token in _TOKEN_RE.findall(str(value or "")) if len(token) > 1}


def _unit(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1:
        number /= 100
    return max(0.0, min(1.0, number))


def _year(candidate: Mapping[str, Any]) -> int | None:
    try:
        year = int(candidate.get("year"))
    except (TypeError, ValueError):
        return None
    return year if 1000 <= year <= 3000 else None


def _dimension_scores(
    candidate: Mapping[str, Any],
    *,
    topic: str,
    keywords: Sequence[str],
    current_year: int,
) -> dict[str, float]:
    text_tokens = _tokens(
        " ".join(
            str(candidate.get(field) or "")
            for field in ("title", "abstract", "venue", "keywords")
        )
    )
    query_tokens = _tokens(" ".join([topic, *keywords]))
    lexical_relevance = len(text_tokens & query_tokens) / max(1, len(query_tokens))
    relevance = _unit(candidate.get("relevance_score"))

    metadata_fields = ("title", "abstract", "authors", "year", "venue")
    completeness = sum(bool(candidate.get(field)) for field in metadata_fields) / len(
        metadata_fields
    )
    source_count = len(candidate.get("sources") or [])
    evidence_fallback = min(1.0, completeness * 0.8 + min(source_count, 3) / 15)
    evidence = _unit(candidate.get("evidence_score"))

    citations = max(0, int(candidate.get("citation_count") or 0))
    impact_fallback = min(1.0, math.log1p(citations) / math.log(1001))
    impact = _unit(candidate.get("impact_score"))

    published_year = _year(candidate)
    novelty_fallback = (
        max(0.0, min(1.0, 1 - (current_year - published_year) / 10))
        if published_year is not None
        else 0.0
    )
    novelty = _unit(candidate.get("novelty_score"))
    has_oa = bool(candidate.get("pdf_url") or candidate.get("oa_url") or candidate.get("is_oa"))

    return {
        "relevance": round(relevance if relevance is not None else lexical_relevance, 6),
        "evidence": round(evidence if evidence is not None else evidence_fallback, 6),
        "impact": round(impact if impact is not None else impact_fallback, 6),
        "novelty": round(novelty if novelty is not None else novelty_fallback, 6),
        "open_access": 1.0 if has_oa else 0.0,
    }


def _normalized_weights(weights: Mapping[str, float] | None) -> dict[str, float]:
    values = dict(DEFAULT_SCORE_WEIGHTS)
    if weights is not None:
        for key in values:
            if key in weights:
                values[key] = max(0.0, float(weights[key]))
    total = sum(values.values())
    if total <= 0:
        raise ValueError("score weights must contain a positive value")
    return {key: value / total for key, value in values.items()}


def _tier(score: float) -> Literal["core", "supporting", "exploratory"]:
    if score >= 0.75:
        return "core"
    if score >= 0.50:
        return "supporting"
    return "exploratory"


def rank_candidates(
    candidates: Sequence[Mapping[str, Any]],
    *,
    topic: str,
    keywords: Sequence[str] = (),
    excluded_keywords: Sequence[str] = (),
    weights: Mapping[str, float] | None = None,
    current_year: int,
    limit: int | None = None,
) -> list[RankedCandidate]:
    """过滤明确排除项，计算分项得分并产生确定性排序。"""

    if limit is not None and limit < 0:
        raise ValueError("limit must not be negative")
    normalized_weights = _normalized_weights(weights)
    exclusions = [_normalized_text(term) for term in excluded_keywords if str(term).strip()]
    ranked: list[RankedCandidate] = []
    for candidate in merge_candidates(candidates):
        searchable = _normalized_text(
            " ".join(str(candidate.get(field) or "") for field in ("title", "abstract", "keywords"))
        )
        if any(term in searchable for term in exclusions):
            continue
        dimensions = _dimension_scores(
            candidate,
            topic=topic,
            keywords=keywords,
            current_year=current_year,
        )
        score = round(
            sum(dimensions[name] * normalized_weights[name] for name in normalized_weights), 6
        )
        reasons = tuple(
            f"{name}={dimensions[name]:.3f} (weight={normalized_weights[name]:.3f})"
            for name in normalized_weights
        )
        ranked.append(
            RankedCandidate(
                identity=candidate_identity(candidate),
                candidate=candidate,
                score=score,
                tier=_tier(score),
                dimensions=dimensions,
                reasons=reasons,
            )
        )

    ranked.sort(
        key=lambda row: (
            -row.score,
            -(_year(row.candidate) or 0),
            _normalized_text(row.candidate.get("title")),
            row.identity,
        )
    )
    return ranked if limit is None else ranked[:limit]
