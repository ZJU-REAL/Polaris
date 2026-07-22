"""池级可见性（P5b 修复）：个人补充入库（零库成员行）的论文，
本人经书架 / 个人库可读（详情 / 子资源 / 个人 wiki 全链路）；
他人（未入架未收藏）与写库端点维持 404。"""

import uuid

from app.core.db import get_sessionmaker
from app.models.paper import Paper
from tests.conftest import register_and_login

RECT = {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.12}


async def _setup(client, *, name="pool-proj", email="pool-alice@example.com"):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


async def _seed_pool_only_paper(**fields) -> str:
    async with get_sessionmaker()() as session:
        paper = Paper(**({"title": "Pool Access Paper", "source": "manual"} | fields))
        session.add(paper)
        await session.commit()
        return str(paper.id)


async def test_shelved_pool_paper_full_reading_chain(client):
    """个人补充论文（零库成员行）入架后：详情 / 笔记 / 划线 / 图片 /
    个人库埋点 / 个人 wiki 全链路对本人可用。"""
    project_id, headers = await _setup(client)
    paper_id = await _seed_pool_only_paper(
        title="Personally Supplemented", abstract="No library membership."
    )
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201, resp.text

    # 详情：course 上下文 = 入架课题；无判断字段但形状完整
    resp = await client.get(f"/api/papers/{paper_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    detail = resp.json()
    assert detail["project_id"] == project_id
    assert detail["status"] == "included"
    assert detail["relevance_score"] is None
    assert detail["wiki_content"] is None and detail["has_wiki"] is False
    assert detail["concepts"] == []

    # 子资源：PDF（可见但无文件 → PDF_NOT_AVAILABLE 而非 PAPER_NOT_FOUND）/ 图片
    resp = await client.get(f"/api/papers/{paper_id}/pdf", headers=headers)
    assert resp.status_code == 404 and resp.json()["detail"] == "PDF_NOT_AVAILABLE"
    resp = await client.get(f"/api/papers/{paper_id}/figures", headers=headers)
    assert resp.status_code == 200 and resp.json() == []

    # 笔记 / 划线
    resp = await client.post(
        f"/api/papers/{paper_id}/notes", json={"content": "库外论文的笔记"}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    resp = await client.get(f"/api/papers/{paper_id}/notes", headers=headers)
    assert [n["content"] for n in resp.json()] == ["库外论文的笔记"]
    resp = await client.post(
        f"/api/papers/{paper_id}/highlights",
        json={"page": 1, "rects": [RECT], "selected_text": "pool highlight"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    resp = await client.get(f"/api/papers/{paper_id}/highlights", headers=headers)
    assert len(resp.json()) == 1

    # 个人状态 + 个人库埋点 / 收藏态
    resp = await client.put(
        f"/api/papers/{paper_id}/my-meta", json={"starred": True}, headers=headers
    )
    assert resp.status_code == 200 and resp.json()["starred"] is True
    resp = await client.post(
        "/api/me/library/visits", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    resp = await client.get(f"/api/me/library/state?paper_id={paper_id}", headers=headers)
    assert resp.status_code == 200 and resp.json()["saved"] is True  # 入架已代收藏

    # 个人 wiki 编译后，书架解析出 personal
    resp = await client.post(
        f"/api/papers/{paper_id}/personal-wiki", json={"topic_id": project_id}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/api/projects/{project_id}/shelf", headers=headers)
    assert resp.json()["items"][0]["wiki_source"] == "personal"


async def test_pool_paper_visible_via_personal_library_after_shelf_removal(client):
    """移出书架后个人库条目仍在 → 仍可读，课题上下文为空。"""
    project_id, headers = await _setup(client, name="pool-proj-2", email="pool-bob@example.com")
    paper_id = await _seed_pool_only_paper(title="Entry Only Paper", arxiv_id="2405.00005")
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201
    resp = await client.delete(f"/api/projects/{project_id}/shelf/{paper_id}", headers=headers)
    assert resp.status_code == 204

    resp = await client.get(f"/api/papers/{paper_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] is None  # 无课题上下文（仅个人库可达）
    resp = await client.get(f"/api/papers/{paper_id}/notes", headers=headers)
    assert resp.status_code == 200


async def test_pool_paper_hidden_from_others_and_write_paths(client):
    project_id, headers = await _setup(client, name="pool-proj-3", email="pool-carol@example.com")
    paper_id = await _seed_pool_only_paper(title="Private Chain Paper")
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    assert resp.status_code == 201

    # 其他用户（未入架未收藏）：详情与子资源一律 404
    stranger = await register_and_login(client, email="pool-stranger@example.com")
    sh = {"Authorization": f"Bearer {stranger}"}
    for url in (
        f"/api/papers/{paper_id}",
        f"/api/papers/{paper_id}/notes",
        f"/api/papers/{paper_id}/highlights",
        f"/api/papers/{paper_id}/figures",
        f"/api/papers/{paper_id}/pdf",
    ):
        resp = await client.get(url, headers=sh)
        assert resp.status_code == 404, url

    # 写库成员行的端点不开池级兜底：对本人也维持 404（应走个人 wiki / 书架操作）
    resp = await client.patch(
        f"/api/papers/{paper_id}", json={"status": "excluded"}, headers=headers
    )
    assert resp.status_code == 404
    resp = await client.post(f"/api/papers/{paper_id}/recompile", headers=headers)
    assert resp.status_code == 404
    resp = await client.delete(f"/api/papers/{paper_id}", headers=headers)
    assert resp.status_code == 404
    async with get_sessionmaker()() as session:
        assert await session.get(Paper, uuid.UUID(paper_id)) is not None
