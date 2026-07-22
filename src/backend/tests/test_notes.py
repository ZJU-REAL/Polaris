"""论文笔记（docs/api-lit.md §2 + P5b 归属拆分）：CRUD + 仅作者可见 +
跨课题共享 + 课题笔记本 + 检索并入 + 导出小节。"""

import io
import uuid
import zipfile

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.paper import PaperNote
from app.models.user import User
from app.services.notes import author_name_of
from tests.conftest import add_paper, membership_of, register_and_login


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


async def test_notes_crud_and_author_only_visibility(client):
    project_id, alice, bob, ids = await _setup(client)

    resp = await client.post(
        f"/api/papers/{ids['p1']}/notes", json={"content": "重点：方法部分"}, headers=alice
    )
    assert resp.status_code == 201, resp.text
    note = resp.json()
    assert note["paper_id"] == ids["p1"]
    assert note["author_name"] == "Alice"  # display_name 优先
    assert "project_id" not in note  # P5b：笔记不再挂项目
    note_id = note["id"]

    # 仅作者可见：bob 的列表只有 bob 自己的笔记，看不到 alice 的
    await client.post(f"/api/papers/{ids['p1']}/notes", json={"content": "第二条"}, headers=bob)
    resp = await client.get(f"/api/papers/{ids['p1']}/notes", headers=bob)
    assert [n["content"] for n in resp.json()] == ["第二条"]
    resp = await client.get(f"/api/papers/{ids['p1']}/notes", headers=alice)
    assert [n["content"] for n in resp.json()] == ["重点：方法部分"]

    # 作者可改
    resp = await client.patch(f"/api/notes/{note_id}", json={"content": "改过了"}, headers=alice)
    assert resp.status_code == 200
    assert resp.json()["content"] == "改过了"

    # 作者可删
    resp = await client.delete(f"/api/notes/{note_id}", headers=alice)
    assert resp.status_code == 204
    resp = await client.get(f"/api/papers/{ids['p1']}/notes", headers=alice)
    assert resp.json() == []

    # author_name 回退 email @ 前部分
    assert author_name_of("", "carol@example.com") == "carol"
    assert author_name_of(None, "dave@example.com") == "dave"


async def test_notes_shared_across_topics_and_survive_library_removal(client):
    """P5b 归属拆分：同一篇论文的笔记跨课题共享；库剔除不删笔记。"""
    project_id, alice, bob, ids = await _setup(client)
    # alice 开第二个课题，并把 p1 也收进第二个课题的方向库
    resp = await client.post("/api/projects", json={"name": "notes-proj-2"}, headers=alice)
    project2_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        from app.models.library_direction import LibraryPaper
        from app.services.libraries import get_library_for_project

        library2 = await get_library_for_project(session, uuid.UUID(project2_id))
        session.add(
            LibraryPaper(
                library_id=library2.id, paper_id=uuid.UUID(ids["p1"]), status="included"
            )
        )
        await session.commit()

    await client.post(
        f"/api/papers/{ids['p1']}/notes", json={"content": "跨课题的笔记"}, headers=alice
    )

    # 两个课题的笔记本都能看到（paper × author，无课题隔离）
    for pid in (project_id, project2_id):
        resp = await client.get(f"/api/projects/{pid}/notes", headers=alice)
        assert resp.status_code == 200
        assert [n["content"] for n in resp.json()["items"]] == ["跨课题的笔记"], pid

    # 从第一个课题的库硬删这篇论文 → 笔记保留（第二个课题照常可见）
    async with get_sessionmaker()() as session:
        membership = await membership_of(session, project_id=project_id, paper_id=ids["p1"])
        await session.delete(membership)
        await session.commit()
    resp = await client.get(f"/api/papers/{ids['p1']}/notes", headers=alice)
    assert [n["content"] for n in resp.json()] == ["跨课题的笔记"]
    resp = await client.get(f"/api/projects/{project2_id}/notes", headers=alice)
    assert resp.json()["total"] == 1
    async with get_sessionmaker()() as session:
        stmt = select(PaperNote).where(PaperNote.paper_id == uuid.UUID(ids["p1"]))
        rows = (await session.execute(stmt)).scalars().all()
        assert len(rows) == 1


async def test_notes_permissions(client):
    project_id, alice, bob, ids = await _setup(client)
    resp = await client.post(
        f"/api/papers/{ids['p1']}/notes", json={"content": "alice 的笔记"}, headers=alice
    )
    note_id = resp.json()["id"]

    # 非作者成员改/删 → 404（P5b 起他人笔记不可见，视为不存在）
    resp = await client.patch(f"/api/notes/{note_id}", json={"content": "x"}, headers=bob)
    assert resp.status_code == 404
    resp = await client.delete(f"/api/notes/{note_id}", headers=bob)
    assert resp.status_code == 404

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
        f"/api/papers/{ids['p2']}/notes", json={"content": "另一篇的量子笔记"}, headers=alice
    )
    await client.post(
        f"/api/papers/{ids['p2']}/notes", json={"content": "bob 私有笔记"}, headers=bob
    )

    # 笔记本只聚合「我的」笔记（bob 的不计入）
    resp = await client.get(f"/api/projects/{project_id}/notes?size=2&page=1", headers=alice)
    body = resp.json()
    assert body["total"] == 4 and len(body["items"]) == 2
    assert body["items"][0]["content"] == "另一篇的量子笔记"  # created_at 倒序
    assert body["items"][0]["paper_title"] == "Second Paper"
    resp = await client.get(f"/api/projects/{project_id}/notes", headers=bob)
    assert resp.json()["total"] == 1
    assert resp.json()["items"][0]["content"] == "bob 私有笔记"

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


async def test_keyword_search_includes_own_note_hits_only(client):
    project_id, alice, bob, ids = await _setup(client)
    await client.post(
        f"/api/papers/{ids['p2']}/notes", json={"content": "提到了量子退火算法"}, headers=alice
    )
    resp = await client.get(
        f"/api/projects/{project_id}/search?q=量子退火&mode=keyword", headers=alice
    )
    titles = [p["title"] for p in resp.json()["papers"]]
    assert titles == ["Second Paper"]  # 仅笔记命中也返回

    # bob 搜同一关键词：alice 的私有笔记不参与命中
    resp = await client.get(
        f"/api/projects/{project_id}/search?q=量子退火&mode=keyword", headers=bob
    )
    assert resp.json()["papers"] == []


async def test_obsidian_export_includes_own_notes_section(client):
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

    # bob 导出：alice 的笔记不出现（只导出请求者本人的）
    resp = await client.get(f"/api/projects/{project_id}/export/obsidian", headers=bob)
    zf_bob = zipfile.ZipFile(io.BytesIO(resp.content))
    bob_md = zf_bob.read("papers/agent-planning-with-rl.md").decode("utf-8")
    assert "## 笔记" not in bob_md
