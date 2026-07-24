"""手动添加文献（docs/api-lit.md §4）：三来源 + 去重 409 + 解析失败/互斥 422，全离线。"""

import uuid

import fakeredis.aioredis
import httpx
import pytest_asyncio
import respx

from app.core.db import get_sessionmaker
from app.models.paper import Paper
from app.services.literature import reset_clients, set_clients
from app.services.literature.arxiv import ArxivClient
from app.services.literature.openalex import OpenAlexClient
from tests.conftest import make_project_with_library, membership_of, register_and_login

ARXIV_FEED_ONE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2406.00001v2</id>
    <title>Autonomous Research Agents</title>
    <summary>We study autonomous research agents.</summary>
    <published>2026-06-01T00:00:00Z</published>
    <updated>2026-06-02T00:00:00Z</updated>
    <author><name>Alice Smith</name></author>
    <category term="cs.LG"/>
  </entry>
</feed>
"""

ARXIV_FEED_EMPTY = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"></feed>
"""

BIBTEX_ENTRY = """@inproceedings{smith2025bench,
  title = {A {Benchmark} for Agents},
  author = {Smith, Alice and Bob Jones},
  year = {2025},
  booktitle = {Proceedings of NeurIPS},
  doi = {10.1000/bench},
  url = {https://example.org/bench},
}
"""


@pytest_asyncio.fixture
async def lit_clients():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    set_clients(
        arxiv=ArxivClient(redis=redis, min_interval=0),
        openalex=OpenAlexClient(redis=redis, mailto="test@example.org"),
    )
    yield
    reset_clients()
    await redis.aclose()


async def _setup(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    # P9c：课题不再自动建库——显式配一条 active 起源库供人工纳入落成员行。
    project_id, _library_id = await make_project_with_library(client, headers, name="manual-proj")
    return project_id, headers


@respx.mock
async def test_add_by_arxiv_id_and_dedupe_409(client, lit_clients):
    project_id, headers = await _setup(client)
    respx.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
        return_value=httpx.Response(200, text=ARXIV_FEED_ONE)
    )
    # 同步请求只建元数据行（PDF 下载/抽取移入后台任务）：此处不下载 PDF
    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"arxiv_id": "2406.00001v2"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "Autonomous Research Agents"
    assert body["status"] == "included"
    assert body["arxiv_id"] == "2406.00001"
    assert body["authors"] == [{"name": "Alice Smith", "affiliations": []}]
    assert body["pdf_available"] is False  # 同步阶段不下载 PDF
    paper_id = body["id"]

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, uuid.UUID(paper_id))
        membership = await membership_of(session, project_id=project_id, paper_id=paper_id)
        assert paper.source == "manual" and membership.status == "included"

    # 项目内按 arxiv_id 去重 → 409 带已有 paper_id
    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"arxiv_id": "2406.00001"}, headers=headers
    )
    assert resp.status_code == 409
    assert resp.json() == {"detail": "PAPER_EXISTS", "paper_id": paper_id}


@respx.mock
async def test_add_by_doi(client, lit_clients):
    project_id, headers = await _setup(client)
    work = {
        "id": "https://openalex.org/W7",
        "title": "Cited Landmark Paper",
        "doi": "https://doi.org/10.1234/landmark",
        "publication_year": 2023,
        "primary_location": {
            "source": {"display_name": "Nature"},
            "landing_page_url": "https://nature.example/landmark",
        },
        "authorships": [{"author": {"display_name": "Eve Chen"}}],
    }
    respx.get(url__regex=r"https://api\.openalex\.org/works/doi:10\.1234/landmark.*").mock(
        return_value=httpx.Response(200, json=work)
    )
    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"doi": "10.1234/landmark"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "Cited Landmark Paper"
    assert body["doi"] == "10.1234/landmark"
    assert body["venue"] == "Nature"
    assert body["year"] == 2023
    assert body["url"] == "https://nature.example/landmark"


async def test_add_by_bibtex_and_dedupe_by_doi(client):
    project_id, headers = await _setup(client)
    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"bibtex": BIBTEX_ENTRY}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "A Benchmark for Agents"  # 花括号剥掉
    assert body["authors"] == [
        {"name": "Smith, Alice", "affiliations": []},
        {"name": "Bob Jones", "affiliations": []},
    ]
    assert body["year"] == 2025
    assert body["venue"] == "Proceedings of NeurIPS"
    assert body["doi"] == "10.1000/bench"
    assert body["url"] == "https://example.org/bench"

    # 同 doi 再加 → 409
    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"bibtex": BIBTEX_ENTRY}, headers=headers
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == "PAPER_EXISTS"


@respx.mock
async def test_add_parse_failures_422(client, lit_clients):
    project_id, headers = await _setup(client)

    # arxiv 查不到
    respx.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
        return_value=httpx.Response(200, text=ARXIV_FEED_EMPTY)
    )
    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"arxiv_id": "9999.99999"}, headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("PARSE_FAILED")

    # bibtex 缺 title / 不合法
    resp = await client.post(
        f"/api/projects/{project_id}/papers",
        json={"bibtex": "@article{nokey,\n  author = {X},\n}"},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("PARSE_FAILED")


async def test_add_scores_relevance_best_effort(client, fake_redis):
    """打分已移入后台任务（fake LLM）：跑完后分数/tldr/scored_at 落库，status 保持 included。"""
    from app.services import paper_enrich

    project_id, headers = await _setup(client)
    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"bibtex": BIBTEX_ENTRY}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["relevance_score"] is None  # 同步响应尚未打分
    assert body["status"] == "included"
    await paper_enrich.await_task(body["task_id"])

    async with get_sessionmaker()() as session:
        membership = await membership_of(session, project_id=project_id, paper_id=body["id"])
        assert membership.relevance_score is not None and membership.relevance_score > 0.6
        assert membership.scored_at is not None
        assert membership.status == "included"  # 人工纳入，打分不改状态


async def test_add_low_score_keeps_included(client, fake_redis):
    """分低绝不改状态：fake LLM 对含 irrelevant 的标题给低分（后台任务），论文仍 included。"""
    from app.services import paper_enrich

    project_id, headers = await _setup(client)
    bibtex = (
        "@article{doe2024irr,\n"
        "  title = {An Irrelevant Study of Something Else},\n"
        "  author = {Doe, John},\n"
        "  year = {2024},\n"
        "}\n"
    )
    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"bibtex": bibtex}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "included"
    await paper_enrich.await_task(body["task_id"])

    async with get_sessionmaker()() as session:
        membership = await membership_of(session, project_id=project_id, paper_id=body["id"])
        assert membership.relevance_score is not None and membership.relevance_score < 0.6
        assert membership.status == "included" and membership.trash_reason is None


async def test_add_llm_failure_still_201(client, monkeypatch):
    """打分是顺带增值：LLM 挂了照样 201，论文落库、分数留空。"""

    class BoomRouter:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("llm down")

    monkeypatch.setattr("app.services.relevance.get_llm_router", lambda: BoomRouter())
    project_id, headers = await _setup(client)
    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"bibtex": BIBTEX_ENTRY}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["relevance_score"] is None
    assert body["status"] == "included"

    async with get_sessionmaker()() as session:
        membership = await membership_of(session, project_id=project_id, paper_id=body["id"])
        assert membership.relevance_score is None and membership.scored_at is None
        assert membership.status == "included"


async def test_add_mutual_exclusion_422(client):
    project_id, headers = await _setup(client)
    for payload in ({}, {"arxiv_id": "2406.00001", "doi": "10.1/x"}):
        resp = await client.post(
            f"/api/projects/{project_id}/papers", json=payload, headers=headers
        )
        assert resp.status_code == 422, payload

    # 非项目成员 404
    other = await register_and_login(client, email="add-outsider@example.com")
    resp = await client.post(
        f"/api/projects/{project_id}/papers",
        json={"bibtex": BIBTEX_ENTRY},
        headers={"Authorization": f"Bearer {other}"},
    )
    assert resp.status_code == 404
