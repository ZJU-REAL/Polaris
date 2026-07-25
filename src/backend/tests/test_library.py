"""个人文献库（issue #108）：浏览上报去重、收藏/取消、列表检索、清空记录、手动添加、越权。"""

import uuid

import fakeredis.aioredis
import httpx
import pytest_asyncio
import respx
from sqlalchemy import func, select

from app.core.db import get_sessionmaker
from app.models.library import UserLibraryEntry
from app.models.library_direction import LibraryPaper
from app.models.paper import Paper
from app.services import paper_enrich
from app.services.literature import reset_clients, set_clients
from app.services.literature.arxiv import ArxivClient
from app.services.literature.openalex import OpenAlexClient
from tests.conftest import add_paper, register_and_login

ARXIV_FEED_ONE = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2408.22222v1</id>
    <title>Manually Added Paper</title>
    <summary>A paper added by hand straight into the personal library.</summary>
    <published>2026-08-01T00:00:00Z</published>
    <author><name>Dora Lin</name></author>
    <category term="cs.IR"/>
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


async def _make_project(client, headers, name="lib-proj"):
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    return resp.json()["id"]


async def _make_paper(project_id: str, **kwargs) -> str:
    async with get_sessionmaker()() as session:
        paper = await add_paper(session, project_id=uuid.UUID(project_id), **kwargs)
        session.add(paper)
        await session.commit()
        return str(paper.id)


async def _entry_count() -> int:
    """个人库条目总行数（每个测试一套干净的表，等同于当前用户的条目数）。"""
    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(func.count()).select_from(UserLibraryEntry))
        ).scalar_one()


async def test_visit_dedups_across_projects(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    proj_a = await _make_project(client, headers, "lib-a")
    proj_b = await _make_project(client, headers, "lib-b")
    # 同一篇论文（相同 arxiv_id）出现在两个方向
    pa = await _make_paper(proj_a, title="Same Paper", arxiv_id="2401.00001", status="included")
    pb = await _make_paper(proj_b, title="Same Paper", arxiv_id="2401.00001", status="included")

    resp = await client.post("/api/me/library/visits", json={"paper_id": pa}, headers=headers)
    assert resp.status_code == 201, resp.text
    resp = await client.post("/api/me/library/visits", json={"paper_id": pb}, headers=headers)
    assert resp.status_code == 201
    entry = resp.json()
    assert entry["visit_count"] == 2
    assert entry["last_paper_id"] == pb  # 快照跟随最近一次浏览

    resp = await client.get("/api/me/library?tab=history", headers=headers)
    assert resp.json()["total"] == 1


async def test_save_unsave_and_state(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _make_project(client, headers)
    paper_id = await _make_paper(project_id, title="Bookmark Me", doi="10.1/abc", status="scored")

    # 未入库时 state 为空
    resp = await client.get(f"/api/me/library/state?paper_id={paper_id}", headers=headers)
    assert resp.json() == {"entry_id": None, "saved": False}

    # 直接收藏（未浏览过）：进收藏 tab，不进浏览记录
    resp = await client.post("/api/me/library", json={"paper_id": paper_id}, headers=headers)
    assert resp.status_code == 201, resp.text
    entry_id = resp.json()["id"]
    assert resp.json()["saved"] is True
    assert resp.json()["visit_count"] == 0
    resp = await client.get(f"/api/me/library/state?paper_id={paper_id}", headers=headers)
    assert resp.json() == {"entry_id": entry_id, "saved": True}
    resp = await client.get("/api/me/library?tab=saved", headers=headers)
    assert resp.json()["total"] == 1
    resp = await client.get("/api/me/library?tab=history", headers=headers)
    assert resp.json()["total"] == 0

    # 取消收藏：条目进回收站（收藏与浏览记录都看不到，回收站里能看到）
    await client.post("/api/me/library/visits", json={"paper_id": paper_id}, headers=headers)
    resp = await client.delete(f"/api/me/library/{entry_id}?mode=unsave", headers=headers)
    assert resp.status_code == 204
    resp = await client.get("/api/me/library?tab=saved", headers=headers)
    assert resp.json()["total"] == 0
    resp = await client.get("/api/me/library?tab=history", headers=headers)
    assert resp.json()["total"] == 0
    resp = await client.get("/api/me/library?tab=trash", headers=headers)
    assert resp.json()["total"] == 1

    # 彻底删除
    resp = await client.delete(f"/api/me/library/{entry_id}?mode=purge", headers=headers)
    assert resp.status_code == 204
    resp = await client.get("/api/me/library?tab=trash", headers=headers)
    assert resp.json()["total"] == 0
    resp = await client.get("/api/me/library?tab=history", headers=headers)
    assert resp.json()["total"] == 0


async def test_save_existing_history_entry(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _make_project(client, headers)
    paper_id = await _make_paper(project_id, title="History First", status="included")

    resp = await client.post("/api/me/library/visits", json={"paper_id": paper_id}, headers=headers)
    entry_id = resp.json()["id"]
    resp = await client.post("/api/me/library", json={"entry_id": entry_id}, headers=headers)
    assert resp.status_code == 201
    assert resp.json()["saved"] is True
    assert resp.json()["id"] == entry_id

    # paper_id / entry_id 只能给一个
    resp = await client.post(
        "/api/me/library", json={"paper_id": paper_id, "entry_id": entry_id}, headers=headers
    )
    assert resp.status_code == 422


async def test_list_search_and_note(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _make_project(client, headers)
    p1 = await _make_paper(
        project_id,
        title="Attention Is All You Need",
        authors=[{"name": "Ashish Vaswani"}],
        status="included",
    )
    p2 = await _make_paper(project_id, title="ResNet", status="included")
    for pid in (p1, p2):
        await client.post("/api/me/library/visits", json={"paper_id": pid}, headers=headers)

    resp = await client.get("/api/me/library?tab=history&q=attention", headers=headers)
    assert resp.json()["total"] == 1
    resp = await client.get("/api/me/library?tab=history&q=vaswani", headers=headers)
    assert resp.json()["total"] == 1  # 作者名也可检索

    entry_id = resp.json()["items"][0]["id"]
    resp = await client.patch(
        f"/api/me/library/{entry_id}", json={"note": "经典必读"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["note"] == "经典必读"


async def test_library_advanced_filters_and_year_sort(client):
    token = await register_and_login(client, email="libadv@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _make_project(client, headers, "lib-adv")
    p_old = await _make_paper(
        project_id,
        title="Old Paper",
        authors=[{"name": "Alice Smith"}],
        affiliations=["Stanford University"],
        year=2015,
        venue="NeurIPS",
        status="included",
    )
    p_mid = await _make_paper(
        project_id,
        title="Mid Paper",
        authors=[{"name": "Bob Jones"}],
        affiliations=["Microsoft Research"],
        year=2018,
        venue="ICML",
        status="included",
    )
    p_new = await _make_paper(
        project_id,
        title="New Paper",
        authors=[{"name": "Carol Lee"}],
        affiliations=["Google DeepMind"],
        year=2021,
        venue="ICLR",
        status="included",
    )
    for pid in (p_old, p_mid, p_new):
        await client.post("/api/me/library/visits", json={"paper_id": pid}, headers=headers)

    async def query(**params):
        merged = {"tab": "history", **params}
        qs = "&".join(f"{k}={v}" for k, v in merged.items())
        resp = await client.get(f"/api/me/library?{qs}", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        return [i["title"] for i in body["items"]], body["total"]

    # author（JSON 文本包含）
    got, _ = await query(author="Bob")
    assert got == ["Mid Paper"]
    # affiliation：条目快照没有机构字段，按软引用的活体论文匹配
    got, total = await query(affiliation="Microsoft")
    assert got == ["Mid Paper"] and total == 1
    got, total = await query(affiliation="Nowhere Institute")
    assert got == [] and total == 0
    # venue
    got, _ = await query(venue="ICLR")
    assert got == ["New Paper"]
    # year 范围（含端点）
    got, total = await query(year_from=2016, year_to=2020)
    assert got == ["Mid Paper"] and total == 1
    got, _ = await query(year_from=2019)
    assert got == ["New Paper"]
    got, _ = await query(year_to=2016)
    assert got == ["Old Paper"]
    # sort=year（降序）
    got, _ = await query(sort="year")
    assert got == ["New Paper", "Mid Paper", "Old Paper"]


async def test_clear_history_keeps_saved(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _make_project(client, headers)
    p1 = await _make_paper(project_id, title="Keep Saved", status="included")
    p2 = await _make_paper(project_id, title="Drop Me", status="included")
    for pid in (p1, p2):
        await client.post("/api/me/library/visits", json={"paper_id": pid}, headers=headers)
    await client.post("/api/me/library", json={"paper_id": p1}, headers=headers)

    resp = await client.delete("/api/me/library/visits", headers=headers)
    assert resp.status_code == 204
    resp = await client.get("/api/me/library?tab=history", headers=headers)
    assert resp.json()["total"] == 0  # 已收藏条目访问统计清零，不再算浏览记录
    resp = await client.get("/api/me/library?tab=saved", headers=headers)
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["visit_count"] == 0


async def test_visit_requires_paper_visibility(client):
    """P5c 起库成员论文全员可读：他人也能浏览/收藏；
    完全不可见的论文（不在任何库、未入架未收藏）仍 404。"""
    token_a = await register_and_login(client, email="owner@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    project_id = await _make_project(client, headers_a)
    paper_id = await _make_paper(project_id, title="Shared Paper", status="included")

    token_b = await register_and_login(client, email="intruder@example.com")
    headers_b = {"Authorization": f"Bearer {token_b}"}
    resp = await client.post(
        "/api/me/library/visits", json={"paper_id": paper_id}, headers=headers_b
    )
    assert resp.status_code == 201
    resp = await client.post("/api/me/library", json={"paper_id": paper_id}, headers=headers_b)
    assert resp.status_code == 201

    # 池级论文（零库成员行、与 b 无个人链路）仍不可见
    async with get_sessionmaker()() as session:
        pool_paper = Paper(title="Invisible Pool Paper", source="manual")
        session.add(pool_paper)
        await session.commit()
        pool_id = str(pool_paper.id)
    resp = await client.post(
        "/api/me/library/visits", json={"paper_id": pool_id}, headers=headers_b
    )
    assert resp.status_code == 404


async def test_wiki_snapshot_and_detail_endpoint(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _make_project(client, headers)
    paper_id = await _make_paper(
        project_id, title="Wiki Paper", wiki_content="# 摘要\n这是 wiki。", status="compiled"
    )

    resp = await client.post("/api/me/library/visits", json={"paper_id": paper_id}, headers=headers)
    entry = resp.json()
    assert "wiki_content" not in entry  # 列表/浏览响应不带 wiki，防撑爆

    resp = await client.get("/api/me/library?tab=history", headers=headers)
    assert "wiki_content" not in resp.json()["items"][0]

    # 详情端点带 wiki 快照；源论文删除后快照仍在
    resp = await client.get(f"/api/me/library/{entry['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["wiki_content"] == "# 摘要\n这是 wiki。"

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, uuid.UUID(paper_id))
        await session.delete(paper)
        await session.commit()
    resp = await client.get(f"/api/me/library/{entry['id']}", headers=headers)
    assert resp.json()["last_paper_id"] is None
    assert resp.json()["wiki_content"] == "# 摘要\n这是 wiki。"


# ---- 手动添加（POST /me/library/import）：arXiv 编号 / DOI / BibTeX 三选一 ----


async def test_import_pool_hit_reuses_paper_and_is_idempotent(client, fake_redis):
    """平台已有这篇 → 直接复用池里的论文，不新建论文行；重复添加也不建第二条条目。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _make_project(client, headers, "lib-import-hit")
    paper_id = await _make_paper(
        project_id,
        title="Pooled Paper",
        arxiv_id="2405.10001",
        pdf_path="/tmp/x.pdf",
        full_text_path="/tmp/x.txt",
        embedding=[0.0] * 1024,
        status="included",
    )

    resp = await client.post(
        "/api/me/library/import", json={"arxiv_id": "2405.10001"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["saved"] is True
    assert body["last_paper_id"] == paper_id
    assert body["visit_count"] == 0
    assert body["task_id"] is None  # 池命中且已处理完整 → 无需后台补全

    # 带版本号再添加一次：还是同一条条目，论文池也没有多出行
    resp = await client.post(
        "/api/me/library/import", json={"arxiv_id": "2405.10001v3"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["id"] == body["id"]
    assert await _entry_count() == 1
    async with get_sessionmaker()() as session:
        papers = (
            (await session.execute(select(Paper).where(Paper.arxiv_id == "2405.10001")))
            .scalars()
            .all()
        )
        assert len(papers) == 1

    resp = await client.get("/api/me/library?tab=saved", headers=headers)
    assert resp.json()["total"] == 1


@respx.mock
async def test_import_pool_miss_fetches_and_saves(client, lit_clients, fake_redis):
    """池里没有 → 抓 arXiv 解析入池（不进任何方向库）并收藏，回传后台补全任务 id。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    respx.get(url__regex=r"https://export\.arxiv\.org/api/query.*").mock(
        return_value=httpx.Response(200, text=ARXIV_FEED_ONE)
    )
    respx.get(url__regex=r"https://arxiv\.org/pdf/.*").mock(return_value=httpx.Response(404))

    resp = await client.post(
        "/api/me/library/import", json={"arxiv_id": "2408.22222v1"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "Manually Added Paper"
    assert body["saved"] is True
    task_id = body["task_id"]
    assert task_id  # 新建论文 → 启动了后台补全（下载/抽取/向量化）

    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, uuid.UUID(body["last_paper_id"]))
        assert paper is not None and paper.source == "manual"
        # 「在池但不在任何库」：手动添加不给任何方向库建成员行
        memberships = (
            (await session.execute(select(LibraryPaper).where(LibraryPaper.paper_id == paper.id)))
            .scalars()
            .all()
        )
        assert memberships == []

    resp = await client.get("/api/me/library?tab=saved", headers=headers)
    assert resp.json()["total"] == 1

    await paper_enrich.await_task(task_id)  # 排空后台任务（pdf 404）


async def test_import_revives_trashed_entry(client, fake_redis):
    """回收站里的同一篇论文再添加一次 = 复活那一行（不报错、也不建第二行）。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _make_project(client, headers, "lib-import-trash")
    await _make_paper(
        project_id,
        title="Trashed Then Added Again",
        arxiv_id="2405.10002",
        pdf_path="/tmp/y.pdf",
        full_text_path="/tmp/y.txt",
        embedding=[0.0] * 1024,
        status="included",
    )

    resp = await client.post(
        "/api/me/library/import", json={"arxiv_id": "2405.10002"}, headers=headers
    )
    entry_id = resp.json()["id"]
    resp = await client.delete(f"/api/me/library/{entry_id}?mode=unsave", headers=headers)
    assert resp.status_code == 204
    resp = await client.get("/api/me/library?tab=trash", headers=headers)
    assert resp.json()["total"] == 1

    resp = await client.post(
        "/api/me/library/import", json={"arxiv_id": "2405.10002"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["id"] == entry_id
    assert body["saved"] is True
    assert body["trashed_at"] is None
    assert await _entry_count() == 1
    resp = await client.get("/api/me/library?tab=trash", headers=headers)
    assert resp.json()["total"] == 0
    resp = await client.get("/api/me/library?tab=saved", headers=headers)
    assert resp.json()["total"] == 1


async def test_import_requires_login_and_exactly_one_source(client):
    resp = await client.post("/api/me/library/import", json={"arxiv_id": "2405.10003"})
    assert resp.status_code == 401

    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    # 三选一：一个都不给 / 给两个都不行
    resp = await client.post("/api/me/library/import", json={}, headers=headers)
    assert resp.status_code == 422
    resp = await client.post(
        "/api/me/library/import",
        json={"arxiv_id": "2405.10003", "doi": "10.1/abc"},
        headers=headers,
    )
    assert resp.status_code == 422
