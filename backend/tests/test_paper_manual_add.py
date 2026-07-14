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
from tests.conftest import register_and_login

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
    resp = await client.post("/api/projects", json={"name": "manual-proj"}, headers=headers)
    return resp.json()["id"], headers


@respx.mock
async def test_add_by_arxiv_id_and_dedupe_409(client, lit_clients):
    project_id, headers = await _setup(client)
    respx.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
        return_value=httpx.Response(200, text=ARXIV_FEED_ONE)
    )
    # PDF 下载失败也不阻塞创建（只记日志）
    respx.get(url__regex=r"https://arxiv\.org/pdf/.*").mock(return_value=httpx.Response(404))

    resp = await client.post(
        f"/api/projects/{project_id}/papers", json={"arxiv_id": "2406.00001v2"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "Autonomous Research Agents"
    assert body["status"] == "included"
    assert body["arxiv_id"] == "2406.00001"
    assert body["authors"] == [{"name": "Alice Smith"}]
    assert body["pdf_available"] is False  # 自动补 PDF 失败不阻塞
    paper_id = body["id"]

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, uuid.UUID(paper_id))
        assert paper.source == "manual" and paper.status == "included"

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
    assert body["authors"] == [{"name": "Smith, Alice"}, {"name": "Bob Jones"}]
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
