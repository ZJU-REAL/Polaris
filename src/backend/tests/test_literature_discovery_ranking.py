"""文献发现查询计划、去重和排序的确定性回归。"""

import pytest

from app.services.literature.discovery_ranking import (
    SourceCapability,
    build_query_plan,
    candidate_identity,
    merge_candidates,
    rank_candidates,
)


def test_query_plan_keeps_requested_years_and_source_capabilities() -> None:
    plan = build_query_plan(
        topic="structural response under impact",
        keywords=["finite element method", "damage mechanics", "damage mechanics"],
        excluded_keywords=["review article"],
        sources=[
            SourceCapability(name="OpenAlex"),
            SourceCapability(
                name="simple", boolean_operators=False, quoted_phrases=False, year_filter=False
            ),
        ],
        start_year=2016,
        end_year=2026,
        per_source_limit=50,
    )

    assert len(plan) == 4
    assert plan[0].source == "openalex"
    assert plan[0].start_year == 2016 and plan[0].end_year == 2026
    assert 'NOT "review article"' in plan[0].query
    assert plan[1].query.count("damage mechanics") == 1
    assert plan[-1].start_year is None and plan[-1].end_year is None
    assert " OR " not in plan[-1].query and "NOT" not in plan[-1].query


def test_query_plan_rejects_invalid_year_window() -> None:
    with pytest.raises(ValueError, match="start_year"):
        build_query_plan(
            topic="impact",
            keywords=[],
            sources=[SourceCapability(name="arxiv")],
            start_year=2026,
            end_year=2016,
        )


def test_candidate_identity_normalizes_doi_before_title_fallback() -> None:
    left = {"title": "Old title", "doi": "https://doi.org/10.1/ABC."}
    right = {"title": "New title", "DOI": "doi:10.1/abc"}
    assert candidate_identity(left) == "doi:10.1/abc"
    assert candidate_identity(left) == candidate_identity(right)


def test_candidate_identity_handles_multilingual_fallback() -> None:
    candidate = {"title": "结构冲击响应", "year": 2026, "authors": [{"name": "张三"}]}
    assert candidate_identity(candidate).startswith("title:")
    assert candidate_identity(candidate) == candidate_identity(dict(candidate))


def test_merge_preserves_richer_metadata_and_all_sources() -> None:
    candidates = [
        {
            "source": "crossref",
            "title": "Impact response",
            "doi": "10.1/test",
            "citation_count": 2,
        },
        {
            "source": "openalex",
            "title": "Impact response",
            "doi": "https://doi.org/10.1/test",
            "abstract": "A sufficiently detailed abstract.",
            "authors": [{"name": "A. Author"}],
            "year": 2025,
            "citation_count": 12,
        },
    ]
    merged = merge_candidates(candidates)
    assert len(merged) == 1
    assert merged[0]["abstract"].startswith("A sufficiently")
    assert merged[0]["citation_count"] == 12
    assert merged[0]["sources"] == ["crossref", "openalex"]


def test_ranking_is_explainable_filtered_and_stable() -> None:
    candidates = [
        {
            "source": "semantic",
            "title": "Dynamic impact response of composite beams",
            "abstract": "Damage mechanics for dynamic impact response.",
            "year": 2026,
            "citation_count": 30,
            "pdf_url": "https://example.test/a.pdf",
            "evidence_score": 0.9,
        },
        {
            "source": "openalex",
            "title": "Book review of structural engineering",
            "abstract": "Dynamic impact response.",
            "year": 2026,
        },
        {
            "source": "arxiv",
            "title": "A background method",
            "abstract": "General mechanics.",
            "year": 2018,
        },
    ]
    kwargs = {
        "topic": "dynamic impact response",
        "keywords": ["damage mechanics", "composite beams"],
        "excluded_keywords": ["book review"],
        "current_year": 2026,
        "limit": 50,
    }
    first = rank_candidates(candidates, **kwargs)
    second = rank_candidates(candidates, **kwargs)

    assert [row.identity for row in first] == [row.identity for row in second]
    assert len(first) == 2
    assert first[0].candidate["title"].startswith("Dynamic impact")
    assert set(first[0].dimensions) == {
        "relevance",
        "evidence",
        "impact",
        "novelty",
        "open_access",
    }
    assert len(first[0].reasons) == 5


def test_explicit_scores_and_weights_override_fallbacks() -> None:
    candidates = [
        {
            "source": "a",
            "title": "Recent but irrelevant",
            "year": 2026,
            "relevance_score": 0.1,
        },
        {
            "source": "b",
            "title": "Older exact evidence",
            "year": 2016,
            "relevance_score": 0.95,
        },
    ]
    ranked = rank_candidates(
        candidates,
        topic="evidence",
        current_year=2026,
        weights={
            "relevance": 1.0,
            "evidence": 0,
            "impact": 0,
            "novelty": 0,
            "open_access": 0,
        },
    )
    assert ranked[0].candidate["title"] == "Older exact evidence"
    assert ranked[0].score == 0.95


def test_limit_zero_returns_no_results() -> None:
    ranked = rank_candidates(
        [{"source": "a", "title": "A result"}],
        topic="result",
        current_year=2026,
        limit=0,
    )
    assert ranked == []
