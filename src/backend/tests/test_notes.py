"""论文笔记（docs/api-lit.md §2）：CRUD + 权限 + 项目笔记本 + 检索并入 + 导出小节。"""

import io
import uuid
import zipfile

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.user import User
from app.services.notes import author_name_of
from tests.conftest import add_paper, register_and_login


async def _setup(client):
    """alice 建项目 + 两篇论文，bob 为项目成员。"""
    alice = await register_and_login(client)
    headers = {"Authorization": f"Bearer {alice}"}
    resp = await client.post("/api/projects", json={"name": "notes-proj"}, headers=headers)
    project_id = resp.json()["id"]

    bob = await register_and_login(client, email="bob@example.com")
    resp = await client.post(
        f"/api/projects/{project_id}/members",
        json={"email": "bob@example.com", "role": "member"},
        headers=headers,
    )
    assert resp.status_code == 204, resp.text

    async with get_sessionmaker()() as session:
        p1 = await add_paper(session,
            project_id=uuid.UUID(project_id),
            title="Agent Planning with RL",
            abstract="Planning for agents.",
            status="compiled",
            wiki_content="## TL;DR\n\n一句话（fake）。\n",
        )
        p2 = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Second Paper",
            status="included",
        )
        session.add_all([p1, p2])
        await session.commit()
        ids = {"p1": str(p1.id), "p2": str(p2.id)}
    bob_headers = {"Authorization": f"Bearer {bob}"}
    return project_id, headers, bob_headers, ids


async def test_notes_crud_and_author_name(client):
    project_id, alice, bob, ids = await _setup(client)

    resp = await client.post(
        f"/api/papers/{ids['p1']}/notes", json={"content": "重点：方法部分"}, headers=alice
    )
    assert resp.status_code == 201, resp.text
    note = resp.json()
    assert note["paper_id"] == ids["p1"] and note["project_id"] == project_id
    assert note["author_name"] == "Alice"  # display_name 优先
    note_id = note["id"]

    # 成员可读（按 created_at 倒序）
    await client.post(f"/api/papers/{ids['p1']}/notes", json={"content": "第二条"}, headers=bob)
    resp = await client.get(f"/api/papers/{ids['p1']}/notes", headers=bob)
    notes = resp.json()
    assert [n["content"] for n in notes] == ["第二条", "重点：方法部分"]

    # 作者可改
    resp = await client.patch(f"/api/notes/{note_id}", json={"content": "改过了"}, headers=alice)
    assert resp.status_code == 200
    assert resp.json()["content"] == "改过了"

    # 作者可删
    resp = await client.delete(f"/api/notes/{note_id}", headers=alice)
    assert resp.status_code == 204
    resp = await client.get(f"/api/papers/{ids['p1']}/notes", headers=alice)
    assert len(resp.json()) == 1

    # author_name 回退 email @ 前部分
    assert author_name_of("", "carol@example.com") == "carol"
    assert author_name_of(None, "dave@example.com") == "dave"


async def test_notes_permissions(client):
    project_id, alice, bob, ids = await _setup(client)
    resp = await client.post(
        f"/api/papers/{ids['p1']}/notes", json={"content": "alice 的笔记"}, headers=alice
    )
    note_id = resp.json()["id"]

    # 非作者成员改/删 → 403
    resp = await client.patch(f"/api/notes/{note_id}", json={"content": "x"}, headers=bob)
    assert resp.status_code == 403
    resp = await client.delete(f"/api/notes/{note_id}", headers=bob)
    assert resp.status_code == 403

    # 非项目成员 → 404（笔记与论文都视为不存在）
    mallory = await register_and_login(client, email="mallory@example.com")
    outsider = {"Authorization": f"Bearer {mallory}"}
    resp = await client.get(f"/api/papers/{ids['p1']}/notes", headers=outsider)
    assert resp.status_code == 404
    resp = await client.patch(f"/api/notes/{note_id}", json={"content": "x"}, headers=outsider)
    assert resp.status_code == 404

    # 平台 admin（bob 提权后）可删非本人笔记
    async with get_sessionmaker()() as session:
        user = (
            await session.execute(select(User).where(User.email == "bob@example.com"))
        ).scalar_one()
        user.role = "admin"
        await session.commit()
    resp = await client.delete(f"/api/notes/{note_id}", headers=bob)
    assert resp.status_code == 204


async def test_project_notebook_pagination_and_search(client):
    project_id, alice, bob, ids = await _setup(client)
    for i in range(3):
        await client.post(
            f"/api/papers/{ids['p1']}/notes", json={"content": f"note-{i} 关于对齐"}, headers=alice
        )
    await client.post(
        f"/api/papers/{ids['p2']}/notes", json={"content": "另一篇的量子笔记"}, headers=bob
    )

    resp = await client.get(f"/api/projects/{project_id}/notes?size=2&page=1", headers=alice)
    body = resp.json()
    assert body["total"] == 4 and len(body["items"]) == 2
    assert body["items"][0]["content"] == "另一篇的量子笔记"  # created_at 倒序
    assert body["items"][0]["paper_title"] == "Second Paper"
    assert body["items"][0]["author_name"] == "Alice"  # bob 注册时 display_name 也是 Alice

    # q 内容搜索 + paper_id 过滤
    resp = await client.get(f"/api/projects/{project_id}/notes?q=量子", headers=alice)
    assert resp.json()["total"] == 1
    resp = await client.get(f"/api/projects/{project_id}/notes?paper_id={ids['p1']}", headers=alice)
    assert resp.json()["total"] == 3

    # 非成员 404
    eve = await register_and_login(client, email="eve@example.com")
    resp = await client.get(
        f"/api/projects/{project_id}/notes", headers={"Authorization": f"Bearer {eve}"}
    )
    assert resp.status_code == 404


async def test_keyword_search_includes_note_hits(client):
    project_id, alice, bob, ids = await _setup(client)
    await client.post(
        f"/api/papers/{ids['p2']}/notes", json={"content": "提到了量子退火算法"}, headers=alice
    )
    resp = await client.get(
        f"/api/projects/{project_id}/search?q=量子退火&mode=keyword", headers=alice
    )
    titles = [p["title"] for p in resp.json()["papers"]]
    assert titles == ["Second Paper"]  # 仅笔记命中也返回


async def test_obsidian_export_includes_notes_section(client):
    project_id, alice, bob, ids = await _setup(client)
    await client.post(
        f"/api/papers/{ids['p1']}/notes", json={"content": "导出验证用笔记"}, headers=alice
    )

    resp = await client.get(f"/api/projects/{project_id}/export/obsidian", headers=alice)
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    paper_md = zf.read("papers/agent-planning-with-rl.md").decode("utf-8")
    assert "## 笔记" in paper_md
    assert "> **Alice** (" in paper_md  # > **{author_name}** ({YYYY-MM-DD})
    assert "导出验证用笔记" in paper_md
    # 无笔记的论文页不加小节
    second_md = zf.read("papers/second-paper.md").decode("utf-8")
    assert "## 笔记" not in second_md
