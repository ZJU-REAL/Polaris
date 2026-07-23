"""M2 API 冒烟：papers / concepts / search / obsidian export / stats（直接种子 DB）。"""

import io
import uuid
import zipfile
from datetime import UTC, datetime

from sqlalchemy import insert

from app.core.db import get_sessionmaker
from app.models.activity import Activity
from app.models.paper import Paper, paper_concepts
from tests.conftest import add_concept, add_paper, register_and_login


async def _setup(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "api-proj"}, headers=headers)
    project_id = uuid.UUID(resp.json()["id"])

    async with get_sessionmaker()() as session:
        p1 = await add_paper(session,
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
        p2 = await add_paper(session,
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
        c1 = await add_concept(session,
            project_id=project_id,
            name="Agent",
            slug="agent",
            definition="能自主感知-决策-行动的智能体。",
            category="method",
        )
        c2 = await add_concept(session,
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

    # 非项目成员也可读（P5c：库成员论文全员可读），但无课题上下文
    other = await register_and_login(client, email="mallory@example.com")
    resp = await client.get(
        f"/api/papers/{ids['p1']}", headers={"Authorization": f"Bearer {other}"}
    )
    assert resp.status_code == 200
    assert resp.json()["project_id"] is None


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
    # p1=compiled 在库内；p2=excluded 在回收站，不计入
    assert stats["papers_total"] == 1
    assert stats["papers_today"] == 1  # 刚插入
    assert stats["ideas_candidate"] == 0
    assert stats["ideas_under_review"] == 0
    assert stats["experiments_active"] == 0
    assert stats["experiments_running"] == 0
    assert stats["manuscripts_total"] == 0
    assert stats["manuscripts_under_review"] == 0
    assert stats["gates_pending"] == 0
    assert stats["recent_activities"][0]["kind"] == "ingest.completed"

    # 非成员看不到
    other = await register_and_login(client, email="eve@example.com")
    resp = await client.get(
        f"/api/projects/{project_id}/stats", headers={"Authorization": f"Bearer {other}"}
    )
    assert resp.status_code == 404


async def test_project_graph(client):
    project_id, headers, ids = await _setup(client)
    resp = await client.get(f"/api/projects/{project_id}/graph", headers=headers)
    assert resp.status_code == 200
    graph = resp.json()

    by_id = {n["id"]: n for n in graph["nodes"]}
    # p1 (compiled) 进图；p2 (excluded) 不进图
    assert ids["p1"] in by_id and ids["p2"] not in by_id
    assert by_id[ids["p1"]]["type"] == "paper"
    # 概念节点（p1 上链了两个概念）
    assert by_id[ids["c1"]]["type"] == "concept"
    assert by_id[ids["c1"]]["count"] == 1
    # 作者节点（Alice）
    authors = [n for n in graph["nodes"] if n["type"] == "author"]
    assert [a["label"] for a in authors] == ["Alice"]

    kinds = {(e["source"], e["target"], e["kind"]) for e in graph["edges"]}
    assert (ids["p1"], ids["c1"], "paper_concept") in kinds
    assert (ids["p1"], authors[0]["id"], "paper_author") in kinds
    assert graph["paper_total"] == 1
    assert graph["truncated"] is False

    # 非成员 404
    other = await register_and_login(client, email="mallory@example.com")
    resp = await client.get(
        f"/api/projects/{project_id}/graph", headers={"Authorization": f"Bearer {other}"}
    )
    assert resp.status_code == 404


async def test_papers_status_group_filters(client):
    """status 组别名（docs/api-lit.md §8.5）：library / pending_compile + 计数口径。"""
    project_id, headers, ids = await _setup(client)

    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        session.add_all(
            [
                await add_paper(
                    session,
                    project_id=uuid.UUID(project_id),
                    title="scored one",
                    status="scored",
                ),
                await add_paper(
                    session,
                    project_id=uuid.UUID(project_id),
                    title="fetched one",
                    status="fetched",
                ),
                await add_paper(
                    session,
                    project_id=uuid.UUID(project_id),
                    title="included one",
                    status="included",
                ),
                await add_paper(
                    session,
                    project_id=uuid.UUID(project_id),
                    title="cand one",
                    status="candidate",
                ),
            ]
        )
        await session.commit()
    # 库内 = scored + fetched + compiled(1, 来自 _setup 的 p1) + included = 4
    resp = await client.get(f"/api/projects/{project_id}/papers?status=library", headers=headers)
    assert resp.json()["total"] == 4
    resp = await client.get(
        f"/api/projects/{project_id}/papers?status=pending_compile", headers=headers
    )
    assert resp.json()["total"] == 2
    # 单状态过滤不受影响
    resp = await client.get(f"/api/projects/{project_id}/papers?status=candidate", headers=headers)
    assert resp.json()["total"] == 1

    # 计数口径：library / pending_compile
    resp = await client.get(f"/api/projects/{project_id}/ingest/state", headers=headers)
    counts = resp.json()["paper_counts"]
    assert counts["library"] == 4 and counts["pending_compile"] == 2
    assert counts["total"] == 6  # 全部（含候选/排除）


async def test_delete_paper_and_batch(client, tmp_path):
    """删除论文（docs/api-lit.md §8.6）：单删 + 批量删 + 成员校验。

    P4 全局内容池语义：删除只摘掉本方向的成员行，内容池行与落盘文件保留
    （其他方向可复用）。"""
    project_id, headers, ids = await _setup(client)

    from app.core.db import get_sessionmaker
    from app.services.literature.pdf_extract import figure_path

    # 给 p1 落一个假 PDF 和图片文件：P4 起删除不清理内容池文件
    pdf = tmp_path / "p1.pdf"
    pdf.write_bytes(b"%PDF-fake")
    fig = figure_path(ids["p1"], 0)
    fig.write_bytes(b"\x89PNG fake")
    async with get_sessionmaker()() as session:
        paper = await session.get(Paper, uuid.UUID(ids["p1"]))
        paper.pdf_path = str(pdf)
        await session.commit()

    # 非成员 404
    other = await register_and_login(client, email="del-outsider@example.com")
    resp = await client.delete(
        f"/api/papers/{ids['p1']}", headers={"Authorization": f"Bearer {other}"}
    )
    assert resp.status_code == 404

    resp = await client.delete(f"/api/papers/{ids['p1']}", headers=headers)
    assert resp.status_code == 204
    # 内容池行与文件保留；本方向视角下论文不复存在
    assert pdf.exists() and fig.exists()
    async with get_sessionmaker()() as session:
        assert await session.get(Paper, uuid.UUID(ids["p1"])) is not None
    resp = await client.get(f"/api/papers/{ids['p1']}", headers=headers)
    assert resp.status_code == 404

    # 批量彻底删除：p2 + 一个不存在的 id（忽略）
    resp = await client.post(
        f"/api/projects/{project_id}/papers/batch-delete",
        json={"paper_ids": [ids["p2"], str(uuid.uuid4())], "hard": True},
        headers=headers,
    )
    assert resp.status_code == 200 and resp.json()["deleted"] == 1
    resp = await client.get(f"/api/projects/{project_id}/papers?status=all", headers=headers)
    # p1/p2 都没了（_setup 只有这两篇）
    resp2 = await client.get(f"/api/projects/{project_id}/papers", headers=headers)
    assert resp2.json()["total"] == 0


async def test_export_citations_by_ids(client):
    project_id, headers, ids = await _setup(client)
    resp = await client.get(
        f"/api/projects/{project_id}/export/citations?format=bibtex&ids={ids['p2']}",
        headers=headers,
    )
    assert resp.status_code == 200
    bib = resp.text
    assert "Benchmark of weaving" in bib and "Agent Planning" not in bib
    # 非法 id → 422
    resp = await client.get(
        f"/api/projects/{project_id}/export/citations?ids=not-a-uuid", headers=headers
    )
    assert resp.status_code == 422


async def test_recompile_links_new_concepts(client):
    """手动编译后自动做单篇概念上链：新 [[双链]] 建词条并关联（修复"概念尚未入库"）。"""
    project_id, headers, ids = await _setup(client)

    resp = await client.post(f"/api/papers/{ids['p2']}/recompile", headers=headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert "[[强化学习]]" in detail["wiki_content"]  # fake librarian 输出

    # 概念已建（强化学习为新词条，Agent 已存在不重复建）并关联到论文
    resp = await client.get(f"/api/projects/{project_id}/concepts", headers=headers)
    names = {c["name"] for c in resp.json()}
    assert "强化学习" in names and "Agent" in names
    resp = await client.get(f"/api/papers/{ids['p2']}", headers=headers)
    linked = {c["name"] for c in resp.json()["concepts"]}
    assert {"Agent", "强化学习"} <= linked


async def test_search_hides_deleted_papers(client):
    """已删除（excluded）/未筛选（candidate）不出现在搜索结果。"""
    project_id, headers, ids = await _setup(client)
    # p2 是 excluded 且标题含 weaving → 关键词搜索不应返回
    resp = await client.get(f"/api/projects/{project_id}/search?q=weaving", headers=headers)
    assert resp.json()["papers"] == []
    # 库内论文照常可搜
    resp = await client.get(f"/api/projects/{project_id}/search?q=Planning", headers=headers)
    assert [p["arxiv_id"] for p in resp.json()["papers"]] == ["2406.10001"]


async def test_trash_soft_delete_restore_and_empty(client):
    """垃圾桶（docs/api-lit.md §8.6）：软删 → 召回 → 清空。"""
    project_id, headers, ids = await _setup(client)

    # 批量软删（默认）：p1 移入垃圾桶
    resp = await client.post(
        f"/api/projects/{project_id}/papers/batch-delete",
        json={"paper_ids": [ids["p1"]]},
        headers=headers,
    )
    assert resp.json()["deleted"] == 1
    resp = await client.get(f"/api/projects/{project_id}/papers?status=library", headers=headers)
    assert resp.json()["total"] == 0  # 库内不再可见
    resp = await client.get(f"/api/projects/{project_id}/papers?status=excluded", headers=headers)
    assert resp.json()["total"] == 2  # p1 + 原本就 excluded 的 p2
    # 垃圾桶原因标签：手动删除的 p1 = manual
    trashed = {p["id"]: p for p in resp.json()["items"]}
    assert trashed[ids["p1"]]["trash_reason"] == "manual"

    # 召回：p1 有 wiki → 回 compiled；原因标签清空
    resp = await client.post(f"/api/papers/{ids['p1']}/restore", headers=headers)
    assert resp.status_code == 200 and resp.json()["status"] == "compiled"
    assert resp.json()["trash_reason"] is None
    # p2 无 wiki 有分数 → 回 scored
    resp = await client.post(f"/api/papers/{ids['p2']}/restore", headers=headers)
    assert resp.json()["status"] == "scored"

    # 再软删 p2 → 清空垃圾桶（彻底删除）
    await client.post(
        f"/api/projects/{project_id}/papers/batch-delete",
        json={"paper_ids": [ids["p2"]]},
        headers=headers,
    )
    resp = await client.post(f"/api/projects/{project_id}/trash/empty", headers=headers)
    assert resp.json()["deleted"] == 1
    resp = await client.get(f"/api/papers/{ids['p2']}", headers=headers)
    assert resp.status_code == 404
    # 库内 p1 不受影响
    resp = await client.get(f"/api/papers/{ids['p1']}", headers=headers)
    assert resp.status_code == 200


async def test_papers_advanced_filters(client):
    """高级检索（docs/api-lit.md §8.7）：作者 / 机构 / 发表时间 / 入库时间。"""
    from datetime import UTC, datetime

    from app.core.db import get_sessionmaker

    project_id, headers, ids = await _setup(client)
    async with get_sessionmaker()() as session:
        session.add(
            await add_paper(session,
                project_id=uuid.UUID(project_id),
                title="Affiliation Paper",
                authors=[{"name": "Carol Zhang"}],
                affiliations=["Zhejiang University", "MIT"],
                year=2024,
                published_at=datetime(2024, 3, 1, tzinfo=UTC),
                status="scored",
            )
        )
        await session.commit()

    base = f"/api/projects/{project_id}/papers"
    resp = await client.get(f"{base}?author=carol", headers=headers)
    assert [p["title"] for p in resp.json()["items"]] == ["Affiliation Paper"]
    resp = await client.get(f"{base}?affiliation=zhejiang", headers=headers)
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["affiliations"] == ["Zhejiang University", "MIT"]
    # 发表时间范围：2024 全年命中新论文；published_at 缺失时按 year 兜底
    resp = await client.get(
        f"{base}?published_from=2024-01-01T00:00:00Z&published_to=2024-12-31T23:59:59Z",
        headers=headers,
    )
    assert resp.json()["total"] == 1
    # 入库时间：未来起点 → 空
    resp = await client.get(f"{base}?created_from=2999-01-01T00:00:00Z", headers=headers)
    assert resp.json()["total"] == 0


async def test_ingest_state_next_sync_at(client):
    """建库与同步：daily + 已建库 → 有下次自动同步时间；否则 null。"""
    project_id, headers, ids = await _setup(client)

    resp = await client.get(f"/api/projects/{project_id}/ingest/state", headers=headers)
    assert resp.json()["next_sync_at"] is None  # 未 bootstrap（无水位线）

    from app.core.db import get_sessionmaker
    from app.models.project import Project

    async with get_sessionmaker()() as session:
        project = await session.get(Project, uuid.UUID(project_id))
        project.definition = {"cadence": "daily"}
        project.ingest_state = {"watermark": "2026-07-15T00:00:00+00:00"}
        await session.commit()

    resp = await client.get(f"/api/projects/{project_id}/ingest/state", headers=headers)
    nxt = resp.json()["next_sync_at"]
    assert nxt is not None and "T03:00:00" in nxt  # 每日 03:00 UTC
