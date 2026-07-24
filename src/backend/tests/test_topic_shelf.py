"""课题「相关研究」书架（P5a）：入架快照 / 幂等 / 移除不动个人库 / 个人补充入库。"""

import uuid

import fakeredis.aioredis
import httpx
import pytest_asyncio
import respx
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library import UserLibraryEntry
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper
from app.models.topic_shelf import TopicPaper
from app.services.literature import reset_clients, set_clients
from app.services.literature.arxiv import ArxivClient
from app.services.literature.openalex import OpenAlexClient
from tests.conftest import add_paper, membership_of, register_and_login

ARXIV_FEED_ONE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2407.12345v1</id>
    <title>Personal Supplement Paper</title>
    <summary>A paper added personally, outside any direction library.</summary>
    <published>2026-07-01T00:00:00Z</published>
    <author><name>Carol Wu</name></author>
    <category term="cs.CL"/>
  </entry>
</feed>
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


async def _setup(client, *, name="shelf-proj", email="alice@example.com"):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


async def _seed_paper(project_id, **fields):
    async with get_sessionmaker()() as session:
        paper = await add_paper(session, project_id=project_id, **fields)
        await session.commit()
        return str(paper.id)


async def test_add_to_shelf_snapshots_wiki_and_saves_to_personal_library(client):
    project_id, headers = await _setup(client)
    paper_id = await _seed_paper(
        project_id,
        title="Shelved Paper",
        arxiv_id="2401.00001",
        status="compiled",
        wiki_content="# 库版解读",
    )

    resp = await client.post(
        f"/api/projects/{project_id}/shelf",
        json={"paper_id": paper_id, "note": "跟我的课题相关"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["paper_id"] == paper_id
    assert body["note"] == "跟我的课题相关"
    assert body["wiki_source"] == "live"
    assert body["wiki_content"] == "# 库版解读"
    assert body["snapshot_at"] is not None
    assert body["source_library_id"] is not None

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(TopicPaper).where(TopicPaper.paper_id == uuid.UUID(paper_id))
            )
        ).scalar_one()
        assert row.wiki_snapshot == "# 库版解读"
        assert str(row.topic_id) == project_id
        # 入架必入个人库：saved 且共享同一次快照
        entry = (
            await session.execute(
                select(UserLibraryEntry).where(
                    UserLibraryEntry.last_paper_id == uuid.UUID(paper_id)
                )
            )
        ).scalar_one()
        assert entry.saved is True
        assert entry.wiki_content == "# 库版解读"


async def test_add_is_idempotent_and_updates_note(client):
    project_id, headers = await _setup(client)
    paper_id = await _seed_paper(project_id, title="Idempotent Paper", status="scored")

    for note in ("first", "second"):
        resp = await client.post(
            f"/api/projects/{project_id}/shelf",
            json={"paper_id": paper_id, "note": note},
            headers=headers,
        )
        assert resp.status_code == 201, resp.text

    resp = await client.get(f"/api/projects/{project_id}/shelf", headers=headers)
    assert resp.status_code == 200
    page = resp.json()
    assert page["total"] == 1
    assert page["items"][0]["note"] == "second"
    assert page["items"][0]["wiki_source"] == "none"

    # ids 端点：勾选态用
    resp = await client.get(f"/api/projects/{project_id}/shelf/ids", headers=headers)
    assert resp.json()["paper_ids"] == [paper_id]


async def test_snapshot_fallback_after_library_removal(client):
    project_id, headers = await _setup(client)
    paper_id = await _seed_paper(
        project_id, title="Vanishing Paper", status="compiled", wiki_content="# 快照兜底"
    )
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201, resp.text

    # 模拟论文被从方向库剔除：删成员行（内容池行保留）
    async with get_sessionmaker()() as session:
        membership = await membership_of(session, project_id=project_id, paper_id=paper_id)
        await session.delete(membership)
        await session.commit()

    resp = await client.get(f"/api/projects/{project_id}/shelf", headers=headers)
    item = resp.json()["items"][0]
    assert item["wiki_source"] == "snapshot"
    assert item["wiki_content"] == "# 快照兜底"
    assert item["snapshot_at"] is not None


async def test_note_patch_and_remove_keeps_personal_library(client):
    project_id, headers = await _setup(client)
    paper_id = await _seed_paper(project_id, title="Removable Paper", status="scored")
    resp = await client.post(
        f"/api/projects/{project_id}/shelf",
        json={"paper_id": paper_id, "note": "初始备注"},
        headers=headers,
    )
    assert resp.status_code == 201

    resp = await client.patch(
        f"/api/projects/{project_id}/shelf/{paper_id}", json={"note": "改后备注"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["note"] == "改后备注"

    resp = await client.delete(f"/api/projects/{project_id}/shelf/{paper_id}", headers=headers)
    assert resp.status_code == 204
    resp = await client.get(f"/api/projects/{project_id}/shelf", headers=headers)
    assert resp.json()["total"] == 0

    # 移出书架不动个人库
    async with get_sessionmaker()() as session:
        entry = (
            await session.execute(
                select(UserLibraryEntry).where(
                    UserLibraryEntry.last_paper_id == uuid.UUID(paper_id)
                )
            )
        ).scalar_one()
        assert entry.saved is True

    # 再删 → 404
    resp = await client.delete(f"/api/projects/{project_id}/shelf/{paper_id}", headers=headers)
    assert resp.status_code == 404


async def test_import_pool_hit_shelves_without_library_membership(client):
    """池命中：他方向已有的论文直接入架，不给本课题隐式库建成员行。"""
    project_id, headers = await _setup(client)
    resp = await client.post("/api/projects", json={"name": "other-proj"}, headers=headers)
    other_project_id = resp.json()["id"]
    paper_id = await _seed_paper(
        other_project_id,
        title="Pooled Paper",
        arxiv_id="2402.00002",
        status="compiled",
        wiki_content="# 他库解读",
    )

    resp = await client.post(
        f"/api/projects/{project_id}/shelf/import",
        json={"arxiv_id": "2402.00002"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["paper_id"] == paper_id
    # 本课题库没有该论文 → 快照取自他库的 wiki
    assert body["wiki_source"] == "live"
    assert body["wiki_content"] == "# 他库解读"

    async with get_sessionmaker()() as session:
        assert await membership_of(session, project_id=project_id, paper_id=paper_id) is None
        # 内容池仍只有一行
        papers = (
            (await session.execute(select(Paper).where(Paper.arxiv_id == "2402.00002")))
            .scalars()
            .all()
        )
        assert len(papers) == 1


@respx.mock
async def test_import_miss_fetches_and_creates_pool_only_paper(client, lit_clients):
    """池未命中：抓 arxiv 解析入池（不建任何 library_papers 行）后入架 + 入个人库。"""
    project_id, headers = await _setup(client)
    respx.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
        return_value=httpx.Response(200, text=ARXIV_FEED_ONE)
    )
    respx.get(url__regex=r"https://arxiv\.org/pdf/.*").mock(return_value=httpx.Response(404))

    resp = await client.post(
        f"/api/projects/{project_id}/shelf/import",
        json={"arxiv_id": "2407.12345v1"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "Personal Supplement Paper"
    assert body["wiki_source"] == "none"
    assert body["source_library_id"] is None  # 个人补充：不挂任何来源库
    paper_id = body["paper_id"]

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, uuid.UUID(paper_id))
        assert paper is not None and paper.source == "manual"
        # 「在池但不在任何库」是合法状态
        memberships = (
            (
                await session.execute(
                    select(LibraryPaper).where(LibraryPaper.paper_id == uuid.UUID(paper_id))
                )
            )
            .scalars()
            .all()
        )
        assert memberships == []
        entry = (
            await session.execute(
                select(UserLibraryEntry).where(
                    UserLibraryEntry.last_paper_id == uuid.UUID(paper_id)
                )
            )
        ).scalar_one()
        assert entry.saved is True

    # 再次 import 同一编号 → 池命中，幂等回到同一书架行
    resp = await client.post(
        f"/api/projects/{project_id}/shelf/import",
        json={"arxiv_id": "2407.12345"},
        headers=headers,
    )
    assert resp.status_code == 201
    assert resp.json()["paper_id"] == paper_id
    resp = await client.get(f"/api/projects/{project_id}/shelf", headers=headers)
    assert resp.json()["total"] == 1


async def test_import_title_only_pool_hit_and_miss(client):
    project_id, headers = await _setup(client)
    paper_id = await _seed_paper(project_id, title="Titled Pool Paper", status="scored")

    resp = await client.post(
        f"/api/projects/{project_id}/shelf/import",
        json={"title": "titled pool paper"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["paper_id"] == paper_id

    # 只有标题且池中没有 → 无法抓取，422
    resp = await client.post(
        f"/api/projects/{project_id}/shelf/import",
        json={"title": "No Such Paper Anywhere"},
        headers=headers,
    )
    assert resp.status_code == 422
    assert resp.json()["detail"].startswith("PARSE_FAILED")

    # 三选一都不给 → 422（pydantic 校验）
    resp = await client.post(
        f"/api/projects/{project_id}/shelf/import", json={}, headers=headers
    )
    assert resp.status_code == 422


async def _shelve(client, headers, project_id, paper_id):
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201, resp.text


async def test_shelf_advanced_search_filters_and_sort(client):
    project_id, headers = await _setup(client, name="shelf-search")
    p_trans = await _seed_paper(
        project_id,
        title="Attention Is All You Need",
        abstract="A transformer architecture.",
        authors=[{"name": "Ashish Vaswani"}],
        affiliations=["Google Brain"],
        year=2017,
        status="compiled",
        relevance_score=0.9,
    )
    p_resnet = await _seed_paper(
        project_id,
        title="Deep Residual Learning",
        abstract="Residual networks for image recognition.",
        authors=[{"name": "Kaiming He"}],
        affiliations=["Microsoft Research"],
        year=2015,
        status="scored",
        relevance_score=0.4,
    )
    p_bert = await _seed_paper(
        project_id,
        title="BERT Pretraining",
        abstract="Bidirectional transformers.",
        authors=[{"name": "Jacob Devlin"}],
        affiliations=["Google AI"],
        year=2019,
        status="compiled",
        relevance_score=0.6,
    )
    for pid in (p_resnet, p_trans, p_bert):  # 入架顺序：resnet 最早、bert 最新
        await _shelve(client, headers, project_id, pid)

    async def query(**params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        resp = await client.get(f"/api/projects/{project_id}/shelf?{qs}", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        return [i["paper_id"] for i in body["items"]], body["total"]

    # q 命中标题/摘要
    got, total = await query(q="Residual")
    assert total == 1 and got == [p_resnet]
    # 作者（JSON 文本包含）
    got, _ = await query(author="Kaiming")
    assert got == [p_resnet]
    # 机构
    got, _ = await query(affiliation="Microsoft")
    assert got == [p_resnet]
    # 年份范围（含端点）
    got, _ = await query(year_from=2016, year_to=2018)
    assert got == [p_trans]
    got, _ = await query(year_from=2018)
    assert got == [p_bert]
    got, _ = await query(year_to=2016)
    assert got == [p_resnet]

    # 默认 sort=added：最新入架在前
    got, _ = await query()
    assert got == [p_bert, p_trans, p_resnet]
    # sort=year（降序，nulls last）
    got, _ = await query(sort="year")
    assert got == [p_bert, p_trans, p_resnet]
    # sort=title（升序）：Attention < BERT < Deep
    got, _ = await query(sort="title")
    assert got == [p_trans, p_bert, p_resnet]
    # sort=relevance（降序）：0.9 > 0.6 > 0.4
    got, _ = await query(sort="relevance")
    assert got == [p_trans, p_bert, p_resnet]


async def test_shelf_filters_by_personal_star_and_reading_status(client):
    project_id, headers = await _setup(client, name="shelf-personal")
    p1 = await _seed_paper(project_id, title="Starred One", status="scored")
    p2 = await _seed_paper(project_id, title="Plain Two", status="scored")
    for pid in (p1, p2):
        await _shelve(client, headers, project_id, pid)

    # 个人视角：给 p1 打星 + 标记在读（PaperUserMeta，方向无关）
    resp = await client.put(
        f"/api/papers/{p1}/my-meta",
        json={"starred": True, "reading_status": "reading"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text

    async def query(**params):
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        resp = await client.get(f"/api/projects/{project_id}/shelf?{qs}", headers=headers)
        assert resp.status_code == 200, resp.text
        return [i["paper_id"] for i in resp.json()["items"]]

    assert await query(starred="true") == [p1]
    assert await query(starred="false") == [p2]
    assert await query(reading_status="reading") == [p1]
    assert sorted(await query(reading_status="unread")) == sorted([p2])


async def test_shelf_requires_project_membership(client):
    project_id, headers = await _setup(client)
    paper_id = await _seed_paper(project_id, title="Members Only", status="scored")
    outsider = await register_and_login(client, email="shelf-outsider@example.com")
    outsider_headers = {"Authorization": f"Bearer {outsider}"}

    for method, url, kwargs in (
        ("get", f"/api/projects/{project_id}/shelf", {}),
        ("get", f"/api/projects/{project_id}/shelf/ids", {}),
        ("post", f"/api/projects/{project_id}/shelf", {"json": {"paper_id": paper_id}}),
        (
            "post",
            f"/api/projects/{project_id}/shelf/import",
            {"json": {"arxiv_id": "2401.99999"}},
        ),
        ("patch", f"/api/projects/{project_id}/shelf/{paper_id}", {"json": {"note": "x"}}),
        ("delete", f"/api/projects/{project_id}/shelf/{paper_id}", {}),
    ):
        resp = await getattr(client, method)(url, headers=outsider_headers, **kwargs)
        assert resp.status_code == 404, (method, url, resp.text)

    # 池中不存在的 paper_id 入架 → 404 PAPER_NOT_FOUND
    resp = await client.post(
        f"/api/projects/{project_id}/shelf",
        json={"paper_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "PAPER_NOT_FOUND"
