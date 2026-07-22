"""个人文献库（issue #108）：浏览上报去重、收藏/取消、列表检索、清空记录、越权。"""

import uuid

from app.core.db import get_sessionmaker
from app.models.paper import Paper
from tests.conftest import add_paper, register_and_login


async def _make_project(client, headers, name="lib-proj"):
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    return resp.json()["id"]


async def _make_paper(project_id: str, **kwargs) -> str:
    async with get_sessionmaker()() as session:
        paper = await add_paper(session, project_id=uuid.UUID(project_id), **kwargs)
        session.add(paper)
        await session.commit()
        return str(paper.id)


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

    # 取消收藏：条目保留（浏览过后进浏览记录）
    await client.post("/api/me/library/visits", json={"paper_id": paper_id}, headers=headers)
    resp = await client.delete(f"/api/me/library/{entry_id}?mode=unsave", headers=headers)
    assert resp.status_code == 204
    resp = await client.get("/api/me/library?tab=saved", headers=headers)
    assert resp.json()["total"] == 0
    resp = await client.get("/api/me/library?tab=history", headers=headers)
    assert resp.json()["total"] == 1

    # 彻底删除
    resp = await client.delete(f"/api/me/library/{entry_id}?mode=purge", headers=headers)
    assert resp.status_code == 204
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


async def test_visit_requires_project_membership(client):
    token_a = await register_and_login(client, email="owner@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    project_id = await _make_project(client, headers_a)
    paper_id = await _make_paper(project_id, title="Private Paper", status="included")

    token_b = await register_and_login(client, email="intruder@example.com")
    headers_b = {"Authorization": f"Bearer {token_b}"}
    resp = await client.post(
        "/api/me/library/visits", json={"paper_id": paper_id}, headers=headers_b
    )
    assert resp.status_code == 404
    resp = await client.post("/api/me/library", json={"paper_id": paper_id}, headers=headers_b)
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
