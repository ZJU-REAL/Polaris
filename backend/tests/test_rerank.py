"""rerank 能力测试：provider（fake 确定性 / openai_compat respx mock）、
router 记账、search semantic 重排路径与降级路径。"""

import json
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm.anthropic import AnthropicProvider
from app.core.llm.fake import FakeProvider
from app.core.llm.openai_compat import OpenAICompatProvider
from app.core.llm.router import LLMRouter
from app.core.security import encrypt_secret
from app.models.llm_config import LLMProviderConfig, LLMUsage, ModelRoute
from app.models.paper import Paper
from app.services import papers as papers_service
from tests.conftest import register_and_login

# ---- provider 层 ----


async def test_fake_rerank_word_overlap_deterministic():
    fake = FakeProvider()
    docs = ["cats and dogs eat food", "agent planning", "planning things"]
    result = await fake.rerank("agent planning", docs, model="fake-rerank")
    assert [i for i, _ in result.results] == [1, 2, 0]  # 词重叠降序，确定性
    scores = [s for _, s in result.results]
    assert scores == sorted(scores, reverse=True)
    assert scores[0] == 1.0 and scores[-1] == 0.0
    assert result.usage["total_tokens"] > 0

    top1 = await fake.rerank("agent planning", docs, model="fake-rerank", top_n=1)
    assert top1.results == result.results[:1]


async def test_base_rerank_not_implemented():
    provider = AnthropicProvider(api_key="sk-x")
    with pytest.raises(NotImplementedError):
        await provider.rerank("q", ["d"], model="claude")
    await provider._client.aclose()


@respx.mock
async def test_openai_compat_rerank_cohere_style():
    route = respx.post("http://lab.test/v1/rerank").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.11},
                    {"index": 1, "relevance_score": 0.92},
                    {"index": 2, "relevance_score": 0.53},
                ],
                "meta": {"billed_units": {"total_tokens": 77}},
            },
        )
    )
    provider = OpenAICompatProvider(base_url="http://lab.test/v1", api_key="sk-test")
    result = await provider.rerank("agent", ["a", "b", "c"], model="BGE-Reranker-V2-M3", top_n=2)
    assert result.results == [(1, 0.92), (2, 0.53)]  # 降序 + top_n 截断
    assert result.usage == {"total_tokens": 77}

    request = route.calls.last.request
    assert request.url.path == "/v1/rerank"  # base_url 已含 /v1，不重复拼接
    assert request.headers["authorization"] == "Bearer sk-test"
    assert json.loads(request.content) == {
        "model": "BGE-Reranker-V2-M3",
        "query": "agent",
        "documents": ["a", "b", "c"],
        "top_n": 2,
    }
    await provider.aclose()


@respx.mock
async def test_openai_compat_rerank_http_error():
    respx.post("http://lab.test/v1/rerank").mock(return_value=httpx.Response(500, text="boom"))
    provider = OpenAICompatProvider(base_url="http://lab.test/v1", api_key="sk-test")
    with pytest.raises(RuntimeError, match="500"):
        await provider.rerank("q", ["d"], model="BGE-Reranker-V2-M3")
    await provider.aclose()


# ---- router 层 ----


async def test_router_rerank_fallback_fake_records_usage(app):
    router = LLMRouter()
    results = await router.rerank("agent planning", ["agent planning", "cats"], top_n=1)
    assert results == [(0, 1.0)]
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(LLMUsage))).scalars().all()
        assert len(rows) == 1
        assert rows[0].stage == "rerank" and rows[0].model == "fake-default"
        assert rows[0].prompt_tokens > 0


@respx.mock
async def test_router_rerank_db_route_billed_units(app):
    respx.post("http://lab.test/v1/rerank").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"index": 0, "relevance_score": 0.9}],
                "meta": {"billed_units": {"total_tokens": 42}},
            },
        )
    )
    async with get_sessionmaker()() as session:
        provider = LLMProviderConfig(
            name="lab-litellm",
            kind="openai_compat",
            base_url="http://lab.test/v1",
            api_key_encrypted=encrypt_secret("sk-lab"),
            enabled=True,
        )
        session.add(provider)
        await session.flush()
        session.add(ModelRoute(stage="rerank", provider_id=provider.id, model="BGE-Reranker-V2-M3"))
        await session.commit()

    router = LLMRouter()
    results = await router.rerank("q", ["only doc"])
    assert results == [(0, 0.9)]
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(LLMUsage))).scalars().all()
        assert len(rows) == 1
        assert rows[0].stage == "rerank" and rows[0].model == "BGE-Reranker-V2-M3"
        assert rows[0].prompt_tokens == 42  # billed_units.total_tokens 优先于估算


# ---- search semantic 重排 / 降级 ----


async def _setup_semantic_project(client, monkeypatch):
    """种子 2 篇论文，并把 pgvector 检索 monkeypatch 成 sqlite 可跑的假实现：
    向量召回故意把无关论文排在前面，以便验证 rerank 生效。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "rerank-proj"}, headers=headers)
    project_id = uuid.UUID(resp.json()["id"])

    async with get_sessionmaker()() as session:
        pa = Paper(
            project_id=project_id,
            title="Cooking pasta at home",
            abstract="A recipe study about food.",
            status="compiled",
        )
        pb = Paper(
            project_id=project_id,
            title="Agent planning",
            abstract=None,
            status="compiled",
        )
        session.add_all([pa, pb])
        await session.commit()
        ordered_ids = [pa.id, pb.id]

    monkeypatch.setattr(papers_service, "semantic_search_supported", lambda session: True)

    async def fake_vector_search(session, *, project_id, query_vector, limit):
        papers = (
            (await session.execute(select(Paper).where(Paper.id.in_(ordered_ids)))).scalars().all()
        )
        by_id = {p.id: p for p in papers}
        return [(by_id[ordered_ids[0]], 0.9), (by_id[ordered_ids[1]], 0.8)][:limit]

    monkeypatch.setattr(papers_service, "semantic_search_papers", fake_vector_search)
    return str(project_id), headers


async def test_search_semantic_reranked(client, monkeypatch):
    project_id, headers = await _setup_semantic_project(client, monkeypatch)
    resp = await client.get(
        f"/api/projects/{project_id}/search",
        params={"q": "agent planning", "mode": "semantic"},
        headers=headers,
    )
    body = resp.json()
    assert body["mode_used"] == "semantic"
    assert body["reranked"] is True
    titles = [p["title"] for p in body["papers"]]
    assert titles == ["Agent planning", "Cooking pasta at home"]  # rerank 翻转向量序
    scores = [p["score"] for p in body["papers"]]
    assert scores[0] == pytest.approx(1.0)  # fake reranker 的词重叠分，而非向量分 0.8/0.9
    assert scores[1] == pytest.approx(0.0)

    async with get_sessionmaker()() as session:
        stages = {u.stage for u in (await session.execute(select(LLMUsage))).scalars().all()}
        assert {"embedding", "rerank"} <= stages  # 两个 stage 均记账


async def test_search_semantic_rerank_degrades_to_vector_scores(client, monkeypatch):
    project_id, headers = await _setup_semantic_project(client, monkeypatch)

    async def broken_rerank(self, query, documents, **kwargs):
        raise RuntimeError("rerank backend down")

    monkeypatch.setattr(LLMRouter, "rerank", broken_rerank)
    resp = await client.get(
        f"/api/projects/{project_id}/search",
        params={"q": "agent planning", "mode": "semantic"},
        headers=headers,
    )
    body = resp.json()
    assert body["mode_used"] == "semantic"  # 仍是语义检索，只是没重排
    assert body["reranked"] is False
    assert [p["title"] for p in body["papers"]] == ["Cooking pasta at home", "Agent planning"]
    assert [p["score"] for p in body["papers"]] == [0.9, 0.8]  # 纯向量分


async def test_search_keyword_response_not_reranked(client, monkeypatch):
    project_id, headers = await _setup_semantic_project(client, monkeypatch)
    resp = await client.get(
        f"/api/projects/{project_id}/search",
        params={"q": "agent", "mode": "keyword"},
        headers=headers,
    )
    body = resp.json()
    assert body["mode_used"] == "keyword"
    assert body["reranked"] is False
