import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.literature.discovery_ranking import rank_candidates
from app.services.literature.retrieval_quality import (
    QueryFamily,
    QueryGenerationError,
    add_retrieval_hit,
    allocate_query_budget,
    compile_source_query,
    fuse_candidates,
    generate_query_families,
    model_rerank,
)


class QueryRouter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def complete(self, stage, messages, **kwargs):
        del messages, kwargs
        assert stage == "extract"
        response = self.responses[self.calls]
        self.calls += 1
        return SimpleNamespace(content=response, model="query-model")


@pytest.mark.asyncio
async def test_query_generation_retries_until_the_plan_is_valid_english():
    router = QueryRouter(
        [
            json.dumps({"queries": [{"seed_id": "core", "purpose": "core", "query": "结构冲击"}]}),
            json.dumps(
                {
                    "queries": [
                        {
                            "seed_id": "core",
                            "purpose": "core",
                            "query": '"structural impact" AND "damage response"',
                        },
                        {
                            "seed_id": "coverage",
                            "purpose": "coverage",
                            "query": '"finite element" OR "failure mode"',
                        },
                    ]
                }
            ),
        ]
    )
    families, snapshot = await generate_query_families(
        llm_router=router,
        topic="结构冲击响应",
        keywords=["有限元", "破坏模式"],
        excluded_keywords=["综述"],
        query_plan=None,
        user_id=None,
        library_id=uuid4(),
    )

    assert router.calls == 2
    assert [item.purpose for item in families] == ["core", "coverage"]
    assert all("结构" not in item.query for item in families)
    assert snapshot == {
        "version": "literature-query-v2",
        "mode": "model",
        "model": "query-model",
        "attempts": 2,
        "validation_errors": ["ValueError: valid English query purposes missing: core, coverage"],
    }


@pytest.mark.asyncio
async def test_query_generation_fallback_does_not_execute_raw_chinese_topic():
    router = QueryRouter(["not json", "still not json", "no structured response"])

    with pytest.raises(QueryGenerationError, match="QUERY_GENERATION_FAILED"):
        await generate_query_families(
            llm_router=router,
            topic="基于视觉模型的结构冲击损伤识别",
            keywords=["结构响应"],
            excluded_keywords=[],
            query_plan=None,
            user_id=None,
            library_id=uuid4(),
        )


def test_source_query_compiler_preserves_boolean_sources_and_simplifies_plain_search():
    query = '("structural impact" AND response) NOT review'

    assert compile_source_query("pubmed", query) == query
    assert compile_source_query("openalex", query) == "structural impact response"
    assert compile_source_query("semantic", query) == "structural impact response"


def test_candidate_budget_is_aggregate_across_sources_and_queries():
    tasks = allocate_query_budget(
        sources=["openalex", "semantic", "arxiv"],
        families=[
            QueryFamily(purpose="core", query="impact response"),
            QueryFamily(purpose="coverage", query="damage mechanics"),
        ],
        candidate_budget=20,
    )

    assert len(tasks) == 6
    assert sum(task.limit for task in tasks) == 20
    assert {task.source for task in tasks} == {"openalex", "semantic", "arxiv"}
    assert {task.purpose for task in tasks} == {"core", "coverage"}


def test_reciprocal_rank_fusion_preserves_query_provenance():
    core = QueryFamily(purpose="core", query="impact response")
    coverage = QueryFamily(purpose="coverage", query="damage mechanics")
    tasks = allocate_query_budget(
        sources=["openalex"], families=[core, coverage], candidate_budget=4
    )
    rows = [
        add_retrieval_hit(
            {"source": "openalex", "title": "Shared", "doi": "10.1/shared"},
            task=tasks[0],
            rank=1,
        ),
        add_retrieval_hit(
            {"source": "semantic", "title": "Shared", "doi": "10.1/shared"},
            task=tasks[1],
            rank=2,
        ),
    ]

    fused = fuse_candidates(rows, executed_query_count=2)

    assert len(fused) == 1
    assert len(fused[0]["metadata"]["retrieval_hits"]) == 2
    assert fused[0]["retrieval_score"] > 0.9


class RerankRouter:
    async def rerank(self, query, documents, **kwargs):
        del query, documents, kwargs
        return [(1, 0.95), (0, 0.1)]

    async def model_name(self, stage, user_id):
        del user_id
        assert stage == "rerank"
        return "rerank-model"


class PartialRerankRouter(RerankRouter):
    async def rerank(self, query, documents, **kwargs):
        del query, documents, kwargs
        return [(1, 0.95)]


@pytest.mark.asyncio
async def test_model_rerank_replaces_relevance_but_keeps_quality_dimensions():
    deterministic = rank_candidates(
        [
            {"source": "a", "title": "Impact response", "year": 2026},
            {"source": "b", "title": "General mechanics", "year": 2025},
        ],
        topic="impact response",
        current_year=2026,
    )
    reranked, snapshot = await model_rerank(
        llm_router=RerankRouter(),
        topic="impact response",
        ranked=deterministic,
        weights=None,
        limit=2,
        user_id=None,
        library_id=uuid4(),
    )

    assert reranked[0].candidate["title"] == "General mechanics"
    assert reranked[0].dimensions["relevance"] == 0.95
    assert reranked[0].dimensions["recency"] == deterministic[1].dimensions["recency"]
    assert snapshot["mode"] == "model"
    assert snapshot["model"] == "rerank-model"


@pytest.mark.asyncio
async def test_partial_model_rerank_never_drops_unreturned_candidates():
    deterministic = rank_candidates(
        [
            {"source": "a", "title": "Impact response", "year": 2026},
            {"source": "b", "title": "General mechanics", "year": 2025},
        ],
        topic="impact response",
        current_year=2026,
    )

    reranked, snapshot = await model_rerank(
        llm_router=PartialRerankRouter(),
        topic="impact response",
        ranked=deterministic,
        weights=None,
        limit=2,
        user_id=None,
        library_id=uuid4(),
    )

    assert len(reranked) == 2
    assert {item.candidate["title"] for item in reranked} == {
        "Impact response",
        "General mechanics",
    }
    assert snapshot["mode"] == "model"
