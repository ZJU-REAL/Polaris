"""共享方向库（P5c）：库读 API 全员可读（非成员 200）、管理端点仍限成员、
库成员论文阅读链路对全员开放（读免费共享，写/管理不放开）。"""

from app.core.db import get_sessionmaker
from tests.conftest import add_concept, add_paper, register_and_login


async def _setup_library(client, *, email="lib-owner@example.com", name="共享方向"):
    """建课题（隐式库）+ 一篇已编译论文 + 一个概念。

    返回 (project_id, headers, paper_id, library_id)。
    """
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    project_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session,
            project_id=project_id,
            title="Shared Library Paper",
            abstract="An abstract about agents.",
            authors=[{"name": "Alice"}],
            status="compiled",
            relevance_score=0.9,
            wiki_content="# 解读\n\n这篇论文讲 [[Agent]]。",
        )
        await add_paper(
            session,
            project_id=project_id,
            title="Excluded Paper",
            status="excluded",
            trash_reason="manual",
        )
        # 概念全平台一份；「这个库有它」按库内论文的关联推导，故挂到库里那篇论文上
        await add_concept(
            session,
            project_id=project_id,
            paper_id=paper.id,
            name="Agent",
            slug="agent",
            category="method",
            definition="能自主行动的智能体。",
        )
        await session.commit()
        paper_id = str(paper.id)
    resp = await client.get("/api/libraries", headers=headers)
    assert resp.status_code == 200, resp.text
    library_id = next(x["id"] for x in resp.json() if x["project_id"] == project_id)
    return project_id, headers, paper_id, library_id


async def _stranger(client, email="lib-stranger@example.com"):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def test_library_list_and_detail(client):
    project_id, headers, _paper_id, library_id = await _setup_library(client)

    resp = await client.get("/api/libraries", headers=headers)
    assert resp.status_code == 200, resp.text
    row = next(x for x in resp.json() if x["id"] == library_id)
    assert row["name"] == "共享方向"
    assert row["paper_count"] == 1  # excluded 不计入
    assert row["concept_count"] == 1

    resp = await client.get(f"/api/libraries/{library_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["project_id"] == project_id
    assert detail["paper_count"] == 1


async def test_library_papers_concepts_search_readable_by_all(client):
    _project_id, _headers, paper_id, library_id = await _setup_library(
        client, email="lib-owner2@example.com"
    )
    stranger = await _stranger(client, email="lib-stranger2@example.com")

    # 论文列表：缺省只列达标论文（excluded 不出现）
    resp = await client.get(f"/api/libraries/{library_id}/papers", headers=stranger)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == paper_id
    assert body["items"][0]["has_wiki"] is True
    # 关键词过滤
    resp = await client.get(f"/api/libraries/{library_id}/papers?q=nothing", headers=stranger)
    assert resp.json()["total"] == 0

    # 概念列表
    resp = await client.get(f"/api/libraries/{library_id}/concepts", headers=stranger)
    assert resp.status_code == 200
    assert [c["name"] for c in resp.json()] == ["Agent"]
    concept_id = resp.json()[0]["id"]

    # 概念详情（id 级端点同样全员可读）
    resp = await client.get(f"/api/concepts/{concept_id}", headers=stranger)
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Agent"

    # 库内检索（关键词）
    resp = await client.get(f"/api/libraries/{library_id}/search?q=agent", headers=stranger)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode_used"] == "keyword"
    assert [p["id"] for p in body["papers"]] == [paper_id]
    assert [c["name"] for c in body["concepts"]] == ["Agent"]

    # 不存在的库 → 404
    resp = await client.get("/api/libraries/00000000-0000-0000-0000-000000000000", headers=stranger)
    assert resp.status_code == 404
