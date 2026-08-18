"""手动添加文献（docs/api-lit.md §4）：三来源 + 去重 409 + 解析失败/互斥 422，全离线。"""

import asyncio
import json
import uuid

import fakeredis.aioredis
import httpx
import pytest
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


async def test_add_by_semantic_scholar_corpus_id(client, monkeypatch):
    from app.services import paper_import

    class _SemanticScholar:
        async def get_paper(self, paper_id):  # noqa: ANN001
            assert paper_id == "CorpusId:13756489"
            return {
                "paperId": "s2-paper-id",
                "title": "A Corpus Identified Paper",
                "abstract": "Imported through Semantic Scholar.",
                "year": 2024,
                "publicationDate": "2024-05-06",
                "venue": "Example Conference",
                "url": "https://www.semanticscholar.org/paper/s2-paper-id",
                "externalIds": {"DOI": "10.1000/corpus", "ArXiv": "2405.00001"},
                "authors": [{"name": "Ada Researcher"}],
            }

    monkeypatch.setattr(paper_import, "get_s2_client", lambda: _SemanticScholar())
    project_id, headers = await _setup(client)
    resp = await client.post(
        f"/api/projects/{project_id}/papers",
        json={"corpus_id": "CorpusId:13756489"},
        headers=headers,
    )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "A Corpus Identified Paper"
    assert body["arxiv_id"] == "2405.00001"
    assert body["doi"] == "10.1000/corpus"
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, uuid.UUID(body["id"]))
        assert paper.external_ids == {
            "s2": "s2-paper-id",
            "corpus_id": "13756489",
            "arxiv": "2405.00001",
            "doi": "10.1000/corpus",
        }


@pytest.mark.parametrize("value", ["", "0", "-1", "abc", "CorpusId:nope"])
def test_invalid_corpus_id_is_rejected(value):
    from app.services.paper_import import ParseFailedError, normalize_corpus_id

    with pytest.raises(ParseFailedError):
        normalize_corpus_id(value)


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
        assert membership.relevance_reason
        assert membership.scored_at is not None
        assert membership.status == "included"  # 人工纳入，打分不改状态


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
async def test_complete_pool_hit_still_scores_new_library_membership(
    client, fake_redis, tmp_path, batch
):
    """共享内容已完整的池命中仍须为新方向库成员打分。"""
    from app.models.paper import new_paper
    from app.services import paper_enrich
    from app.services.libraries import get_membership
    from app.services.paper_import import pool_dedup_key
    from tests.vector_helpers import set_paper_vector

    project_id, headers = await _setup(client)
    async with get_sessionmaker()() as session:
        from app.services.libraries import get_library_for_project

        library = await get_library_for_project(session, uuid.UUID(project_id))
        assert library is not None
        library_id = library.id

        suffix = "batch" if batch else "single"
        title = f"Complete Pool Paper {suffix}"
        doi = f"10.1000/complete-{suffix}"
        pdf_path = tmp_path / f"{suffix}.pdf"
        text_path = tmp_path / f"{suffix}.txt"
        pdf_path.write_bytes(b"%PDF-1.4\n")
        text_path.write_text("Complete paper full text.", encoding="utf-8")
        paper = new_paper(
            source="manual",
            dedup_key=pool_dedup_key(
                arxiv_id=None,
                doi=doi,
                title=title,
                year=2025,
                authors=[{"name": "Test Author"}],
            ),
            title=title,
            authors=[{"name": "Test Author"}],
            affiliations=["Test Lab"],
            abstract="A relevant completed pool paper.",
            year=2025,
            doi=doi,
            pdf_path=str(pdf_path),
            full_text_path=str(text_path),
        )
        session.add(paper)
        await session.flush()
        paper_id = paper.id
        await set_paper_vector(session, paper_id)

    bibtex = (
        f"@article{{complete-{suffix},\n"
        f" title={{{title}}},\n author={{Test Author}},\n year={{2025}},\n doi={{{doi}}}\n}}"
    )
    if batch:
        resp = await client.post(
            f"/api/libraries/{library_id}/paper-imports/batch",
            json={"items": [{"bibtex": bibtex}]},
            headers=headers,
        )
    else:
        resp = await client.post(
            f"/api/libraries/{library_id}/papers",
            json={"bibtex": bibtex},
            headers=headers,
        )
    assert resp.status_code in (201, 202), resp.text
    assert resp.json()["task_id"]
    await paper_enrich.await_task(resp.json()["task_id"])

    async with get_sessionmaker()() as session:
        membership = await get_membership(
            session, library_id=library_id, paper_id=paper_id
        )
        assert membership is not None
        assert membership.relevance_score is not None
        assert membership.relevance_reason
        assert membership.scored_at is not None


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


async def test_batch_add_is_partial_and_reports_each_item(client, fake_redis):
    """批量导入逐项提交：有效、无效、重复项互不回滚，SSE 给出完整汇总。"""
    from app.core.events import paper_task_log_key
    from app.services import paper_enrich

    project_id, headers = await _setup(client)
    resp = await client.post(
        f"/api/projects/{project_id}/paper-imports/batch",
        json={
            "items": [
                {"bibtex": BIBTEX_ENTRY},
                {"bibtex": "@article{missing, author={Nobody}}"},
                {"bibtex": BIBTEX_ENTRY},
            ]
        },
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["total"] == 3 and body["task_id"]
    await paper_enrich.await_task(body["task_id"])

    events = [
        json.loads(raw)
        for raw in await fake_redis.lrange(paper_task_log_key(body["task_id"]), 0, -1)
    ]
    item_events = [event["data"] for event in events if event["event"] == "batch_item"]
    assert [item["status"] for item in item_events] == ["created", "invalid", "existing"]
    assert item_events[0]["paper_id"] == item_events[2]["paper_id"]
    assert "title" in item_events[0]
    assert item_events[1]["error"]
    assert any(event["event"] == "batch_enriched" for event in events)
    assert events[-1] == {
        "event": "done",
        "data": {"total": 3, "created": 1, "existing": 1, "invalid": 1, "failed": 0},
    }


async def test_batch_endpoint_preserves_corpus_id(client, monkeypatch):
    from app.services import paper_enrich

    captured: dict[str, object] = {}

    async def _launch(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return "corpus-batch-task"

    monkeypatch.setattr(paper_enrich, "launch_paper_batch_import", _launch)
    project_id, headers = await _setup(client)
    resp = await client.post(
        f"/api/projects/{project_id}/paper-imports/batch",
        json={"items": [{"corpus_id": "13756489"}]},
        headers=headers,
    )

    assert resp.status_code == 202, resp.text
    assert captured["items"] == [
        {"arxiv_id": None, "doi": None, "corpus_id": "13756489", "bibtex": None}
    ]


async def test_library_batch_add_endpoint_and_limit(client, fake_redis):
    """独立的库作用域入口复用同一任务；服务端硬限制最多 50 项。"""
    from app.services import paper_enrich

    token = await register_and_login(client, email="library-batch@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    _project_id, library_id = await make_project_with_library(
        client, headers, name="library-batch"
    )
    resp = await client.post(
        f"/api/libraries/{library_id}/paper-imports/batch",
        json={"items": [{"bibtex": BIBTEX_ENTRY}]},
        headers=headers,
    )
    assert resp.status_code == 202, resp.text
    await paper_enrich.await_task(resp.json()["task_id"])

    too_many = [{"arxiv_id": f"2406.{index:05d}"} for index in range(51)]
    resp = await client.post(
        f"/api/libraries/{library_id}/paper-imports/batch",
        json={"items": too_many},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_resolve_anchor_batch_keeps_order_and_item_errors(client, monkeypatch):
    """批量锚点解析一次返回全部结果，失败项不让整个请求变成 422。"""
    from app.services import paper_import

    async def _fake(arxiv_ids):  # noqa: ANN001
        assert arxiv_ids == ["2005.11401", "9999.99999"]
        return [
            {
                "arxiv_id": "2005.11401",
                "title": "Retrieval-Augmented Generation",
                "year": 2020,
                "authors": [{"name": "Lewis"}],
            },
            {"arxiv_id": "9999.99999", "error": "not found"},
        ]

    monkeypatch.setattr(paper_import, "resolve_arxiv_fields_batch", _fake)
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    resp = await client.post(
        "/api/papers/resolve-batch",
        json={"arxiv_ids": ["2005.11401", "9999.99999"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert [item["index"] for item in items] == [0, 1]
    assert items[0]["title"] == "Retrieval-Augmented Generation"
    assert items[1]["error"] == "not found" and items[1]["title"] == ""


async def test_resolve_anchor_batch_uses_one_arxiv_request_and_falls_back(monkeypatch):
    """锚点批量解析应合并 arXiv 请求，并只为缺失项调用 OpenAlex。"""
    from app.services import paper_import

    class _Arxiv:
        calls: list[list[str]] = []

        async def fetch_by_ids(self, arxiv_ids):  # noqa: ANN001
            self.calls.append(arxiv_ids)
            return [
                {
                    "arxiv_id": "2005.11401",
                    "title": "RAG",
                    "authors": [{"name": "Lewis"}],
                    "year": 2020,
                }
            ]

    class _OpenAlex:
        calls: list[str] = []

        async def get_by_arxiv(self, arxiv_id):  # noqa: ANN001
            self.calls.append(arxiv_id)
            if arxiv_id == "2406.00001":
                return {"title": "Fallback title", "year": 2024}
            return None

    arxiv = _Arxiv()
    openalex = _OpenAlex()
    monkeypatch.setattr(paper_import, "get_arxiv_client", lambda: arxiv)
    monkeypatch.setattr(paper_import, "get_openalex_client", lambda: openalex)

    results = await paper_import.resolve_arxiv_fields_batch(
        ["2005.11401v2", "2406.00001", "9999.99999"]
    )
    assert arxiv.calls == [["2005.11401", "2406.00001", "9999.99999"]]
    assert openalex.calls == ["2406.00001", "9999.99999"]
    assert [result["arxiv_id"] for result in results] == [
        "2005.11401",
        "2406.00001",
        "9999.99999",
    ]
    assert results[1]["title"] == "Fallback title"
    assert results[2]["error"] == "arxiv 上查不到编号 9999.99999"


async def test_resolve_by_arxiv_id_fills_in_the_title(client, monkeypatch):
    """锚点论文只填 arXiv id，题目由系统补上。

    路由必须排在 /papers/{paper_id} **之前**，否则 "resolve" 会被当成 paper_id
    去解析 UUID，直接 422——这条断言就是在守那个顺序。
    """
    from app.services import paper_import

    async def _fake(arxiv_id=None, doi=None, bibtex=None):
        return {"arxiv_id": "2005.11401", "title": "Retrieval-Augmented Generation",
                "year": 2020, "authors": [{"name": "Lewis"}, {"name": "Perez"}]}

    monkeypatch.setattr(paper_import, "resolve_fields", _fake)
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}

    resp = await client.get(
        "/api/papers/resolve", params={"arxiv_id": "2005.11401"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "Retrieval-Augmented Generation"
    assert body["arxiv_id"] == "2005.11401"
    assert body["authors"] == ["Lewis", "Perez"]


async def test_resolve_unknown_arxiv_id_is_422(client, monkeypatch):
    """解析不出时给 422，前端保留 id、题目留空即可，不该 500。"""
    from app.services import paper_import

    async def _fail(arxiv_id=None, doi=None, bibtex=None):
        raise paper_import.ParseFailedError("nope")

    monkeypatch.setattr(paper_import, "resolve_fields", _fail)
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    resp = await client.get(
        "/api/papers/resolve", params={"arxiv_id": "9999.99999"}, headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "ARXIV_ID_NOT_RESOLVED"


async def test_arxiv_throttling_falls_back_to_openalex(monkeypatch):
    """arXiv 限流时改用 OpenAlex 取元数据。

    线上就是这么坏的：arXiv 429 → ArxivRateLimitedError 一路抛到端点 → 用户看到一句
    「Internal Server Error」。字段会少一点（摘要常缺），但**能加进来的半篇远胜于
    加不进来**——用户手里就是那个编号，让他因为上游限流干等着，等于把别人的故障
    转嫁给他。
    """
    from app.services import paper_import
    from app.services.literature.arxiv import ArxivRateLimitedError

    class _Throttled:
        async def fetch_by_ids(self, ids):  # noqa: ANN001
            raise ArxivRateLimitedError("429 from arXiv")

    class _OpenAlex:
        async def get_by_arxiv(self, arxiv_id):  # noqa: ANN001
            return {"title": "兜底拿到的标题", "year": 2026, "url": "https://example.org/x"}

    monkeypatch.setattr(paper_import, "get_arxiv_client", lambda: _Throttled())
    monkeypatch.setattr(paper_import, "get_openalex_client", lambda: _OpenAlex())

    fields = await paper_import.resolve_fields(arxiv_id="2607.04425")
    assert fields["title"] == "兜底拿到的标题"
    assert fields["arxiv_id"] == "2607.04425"


async def test_both_sources_down_is_a_parse_error_not_a_crash(monkeypatch):
    """两边都查不到时给出可读的错误，而不是把异常抛穿。"""
    from app.services import paper_import
    from app.services.literature.arxiv import ArxivRateLimitedError

    class _Throttled:
        async def fetch_by_ids(self, ids):  # noqa: ANN001
            raise ArxivRateLimitedError("429 from arXiv")

    class _Empty:
        async def get_by_arxiv(self, arxiv_id):  # noqa: ANN001
            return None

    monkeypatch.setattr(paper_import, "get_arxiv_client", lambda: _Throttled())
    monkeypatch.setattr(paper_import, "get_openalex_client", lambda: _Empty())

    with pytest.raises(paper_import.ParseFailedError) as excinfo:
        await paper_import.resolve_fields(arxiv_id="2607.04425")
    assert "限流" in str(excinfo.value)


def test_enrich_semaphore_survives_a_second_event_loop():
    """补全并发闸不能是模块级单例——争用过一次就绑死在那个循环上。

    ``asyncio.Semaphore`` 只在真的需要排队时才记住自己的循环，所以「创建即复用」
    看起来没问题，直到某个用例的批量导入把它用到争用，下一个用例换了新循环再用
    就抛 ``RuntimeError: ... is bound to a different event loop``——而且报在后一个
    用例头上，看起来像它自己的毛病。生产是单循环碰不到，测试必踩。
    """
    import asyncio

    from app.services.paper_enrich import _ENRICH_CONCURRENCY, _enrich_semaphore

    async def contend() -> list[int]:
        async def one(i: int) -> int:
            async with _enrich_semaphore():
                await asyncio.sleep(0)
                return i

        # 并发数必须超过闸门宽度，否则根本不会排队，也就测不到绑定
        return await asyncio.gather(*(one(i) for i in range(_ENRICH_CONCURRENCY + 2)))

    assert asyncio.run(contend()) == list(range(_ENRICH_CONCURRENCY + 2))
    assert asyncio.run(contend()) == list(range(_ENRICH_CONCURRENCY + 2)), "换个循环就用不了了"


async def test_batch_anchor_resolution_gives_up_instead_of_hanging(monkeypatch):
    """逐项兜底有墙钟预算；超了把剩下的记成查不到，不把同步请求拖到网关超时。

    arXiv 一限流，缺失项就变成逐个打 OpenAlex。50 个锚点串行请求远超网关耐心，
    用户看到的是转圈到断线、一条都拿不到——那还不如拿回大部分加几条明确的失败。
    """
    from app.services import paper_import as svc

    async def never_returns_quickly(arxiv_id: str):
        await asyncio.sleep(0.05)
        raise svc.ParseFailedError(f"nope {arxiv_id}")

    class _RateLimited:
        async def fetch_by_ids(self, ids):
            raise svc.ArxivRateLimitedError("simulated")

    monkeypatch.setattr(svc, "get_arxiv_client", lambda: _RateLimited())
    monkeypatch.setattr(svc, "_fields_from_openalex_arxiv", never_returns_quickly)
    monkeypatch.setattr(svc, "_BATCH_FALLBACK_BUDGET_SECONDS", 0.12)

    ids = [f"2608.{i:05d}" for i in range(30)]
    results = await svc.resolve_arxiv_fields_batch(ids)

    assert len(results) == len(ids), "顺序与条数必须和输入一一对应"
    assert all(r.get("error") for r in results), "这一批全都该带错误"
    timed_out = [r for r in results if "解析超时" in str(r.get("error"))]
    assert timed_out, "预算用尽后剩下的该直接记成超时，而不是继续逐个等"
    assert len(timed_out) < len(ids), "预算没用完之前该老老实实地查"
