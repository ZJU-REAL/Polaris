"""M2 API 冒烟：papers / concepts / search / obsidian export / stats（直接种子 DB）。"""

import io
import uuid
import zipfile
from datetime import UTC, datetime

from sqlalchemy import insert

from app.core.db import get_sessionmaker
from app.models.activity import Activity
from app.models.paper import Concept, Paper, paper_concepts
from tests.conftest import register_and_login


async def _setup(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "api-proj"}, headers=headers)
    project_id = uuid.UUID(resp.json()["id"])

    async with get_sessionmaker()() as session:
        p1 = Paper(
            project_id=project_id,
            source="arxiv",
            arxiv_id="2406.10001",
            title="Agent Planning with RL",
            authors=[{"name": "Alice"}],
            abstract="Planning for research agents.",
            year=2026,
            venue="cs.LG",
            url="https://arxiv.org/abs/2406.10001",
            published_at=datetime(2026, 6, 1, tzinfo=UTC),
            relevance_score=0.9,
            tldr="一句话总结",
            wiki_content="## TL;DR\n\n使用 [[Agent]] 与 [[规划]] 的方法。\n",
            status="compiled",
        )
        p2 = Paper(
            project_id=project_id,
            source="arxiv",
            arxiv_id="2406.10002",
            title="Benchmark of weaving",
            authors=["Bob"],  # 历史格式：字符串列表也要能读出
            abstract="Nothing about ml agents here.",
            year=2026,
            published_at=datetime(2026, 5, 1, tzinfo=UTC),
            relevance_score=0.3,
            status="excluded",
        )
        c1 = Concept(
            project_id=project_id,
            name="Agent",
            slug="agent",
            definition="能自主感知-决策-行动的智能体。",
            category="method",
        )
        c2 = Concept(
            project_id=project_id,
            name="规划",
            slug="规划",
            definition="把目标分解为行动序列的过程。",
            category="methodology",
        )
        session.add_all([p1, p2, c1, c2])
        await session.flush()
        await session.execute(
            insert(paper_concepts).values(
                [
                    {"paper_id": p1.id, "concept_id": c1.id},
                    {"paper_id": p1.id, "concept_id": c2.id},
                ]
            )
        )
        session.add(
            Activity(
                project_id=project_id,
                actor="agent:librarian",
                kind="ingest.completed",
                message="文献调研完成：本次编译 1 篇 wiki 页",
            )
        )
        await session.commit()
        ids = {"p1": str(p1.id), "p2": str(p2.id), "c1": str(c1.id), "c2": str(c2.id)}
    return str(project_id), headers, ids


async def test_papers_list_filter_sort_paginate(client):
    project_id, headers, ids = await _setup(client)

    resp = await client.get(f"/api/projects/{project_id}/papers", headers=headers)
    body = resp.json()
    assert body["total"] == 2 and body["page"] == 1 and body["size"] == 20
    assert body["items"][0]["relevance_score"] == 0.9  # 默认按相关性降序
    assert body["items"][0]["authors"] == [{"name": "Alice"}]
    assert body["items"][1]["authors"] == [{"name": "Bob"}]  # 字符串作者归一化
    assert body["items"][0]["has_wiki"] is True

    resp = await client.get(f"/api/projects/{project_id}/papers?status=compiled", headers=headers)
    assert resp.json()["total"] == 1

    resp = await client.get(f"/api/projects/{project_id}/papers?q=weaving", headers=headers)
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["arxiv_id"] == "2406.10002"

    resp = await client.get(
        f"/api/projects/{project_id}/papers?sort=-published_at&size=1&page=2", headers=headers
    )
    body = resp.json()
    assert body["total"] == 2 and len(body["items"]) == 1
    assert body["items"][0]["arxiv_id"] == "2406.10002"  # 第二新


async def test_paper_detail_and_manual_status(client):
    project_id, headers, ids = await _setup(client)

    resp = await client.get(f"/api/papers/{ids['p1']}", headers=headers)
    detail = resp.json()
    assert detail["abstract"].startswith("Planning")
    assert "[[Agent]]" in detail["wiki_content"]
    assert detail["pdf_available"] is False
    assert {c["name"] for c in detail["concepts"]} == {"Agent", "规划"}

    # 人工排除 / 纳入
    resp = await client.patch(
        f"/api/papers/{ids['p2']}", json={"status": "included"}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "included"
    resp = await client.patch(
        f"/api/papers/{ids['p2']}", json={"status": "candidate"}, headers=headers
    )
    assert resp.status_code == 422  # 只允许 included|excluded

    # 非项目成员 → 404
    other = await register_and_login(client, email="mallory@example.com")
    resp = await client.get(
        f"/api/papers/{ids['p1']}", headers={"Authorization": f"Bearer {other}"}
    )
    assert resp.status_code == 404


async def test_concepts_list_and_detail(client):
    project_id, headers, ids = await _setup(client)

    resp = await client.get(f"/api/projects/{project_id}/concepts", headers=headers)
    concepts = resp.json()
    assert {c["name"] for c in concepts} == {"Agent", "规划"}
    agent = next(c for c in concepts if c["name"] == "Agent")
    assert agent["paper_count"] == 1
    assert agent["category"] == "method"

    resp = await client.get(
        f"/api/projects/{project_id}/concepts?category=methodology", headers=headers
    )
    assert [c["name"] for c in resp.json()] == ["规划"]
    resp = await client.get(f"/api/projects/{project_id}/concepts?q=age", headers=headers)
    assert [c["name"] for c in resp.json()] == ["Agent"]

    resp = await client.get(f"/api/concepts/{ids['c1']}", headers=headers)
    detail = resp.json()
    assert detail["definition"].startswith("能自主")
    assert [p["title"] for p in detail["papers"]] == ["Agent Planning with RL"]
    assert [r["name"] for r in detail["related"]] == ["规划"]  # 共现于同一论文


async def test_search_keyword_and_semantic_fallback(client):
    project_id, headers, ids = await _setup(client)

    resp = await client.get(
        f"/api/projects/{project_id}/search?q=agent&mode=keyword", headers=headers
    )
    body = resp.json()
    assert body["mode_used"] == "keyword"
    titles = [p["title"] for p in body["papers"]]
    assert "Agent Planning with RL" in titles
    assert titles[0] == "Agent Planning with RL"  # 标题命中排最前
    assert [c["name"] for c in body["concepts"]] == ["Agent"]
    assert all("score" in p for p in body["papers"])

    # sqlite 下 semantic 回退 keyword，并如实回报 mode_used
    resp = await client.get(
        f"/api/projects/{project_id}/search?q=agent&mode=semantic", headers=headers
    )
    assert resp.json()["mode_used"] == "keyword"


async def test_obsidian_export_zip(client):
    project_id, headers, ids = await _setup(client)
    resp = await client.get(f"/api/projects/{project_id}/export/obsidian", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]

    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    names = set(zf.namelist())
    assert "index.md" in names and "trends.md" in names
    assert "papers/agent-planning-with-rl.md" in names
    assert "concepts/agent.md" in names and "concepts/规划.md" in names

    paper_md = zf.read("papers/agent-planning-with-rl.md").decode("utf-8")
    assert paper_md.startswith("---")
    assert '"2406.10001"' in paper_md  # frontmatter arxiv_id
    assert "[[Agent]]" in paper_md  # 正文保留双链
    concept_md = zf.read("concepts/agent.md").decode("utf-8")
    assert "category:" in concept_md
    assert "[[agent-planning-with-rl]]" in concept_md
    index_md = zf.read("index.md").decode("utf-8")
    assert "[[agent-planning-with-rl]]" in index_md


async def test_project_stats(client):
    project_id, headers, ids = await _setup(client)
    resp = await client.get(f"/api/projects/{project_id}/stats", headers=headers)
    stats = resp.json()
    assert stats["papers_total"] == 2
    assert stats["papers_today"] == 2  # 刚插入
    assert stats["ideas_candidate"] == 0
    assert stats["gates_pending"] == 0
    assert stats["recent_activities"][0]["kind"] == "ingest.completed"

    # 非成员看不到
    other = await register_and_login(client, email="eve@example.com")
    resp = await client.get(
        f"/api/projects/{project_id}/stats", headers={"Authorization": f"Bearer {other}"}
    )
    assert resp.status_code == 404
