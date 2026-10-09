"""论文笔记（docs/task-system.md §7（原 api-lit.md §2） + P5b 归属拆分）：CRUD + 仅作者可见 +
跨课题共享 + 课题笔记本 + 检索并入 + 导出小节。"""

import io
import uuid
import zipfile

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.paper import PaperNote
from app.services.notes import author_name_of
from tests.conftest import add_paper, membership_of, register_and_login


async def _setup(client):
    """本地用户建项目 + 两篇论文。"""
    alice = await register_and_login(client)
    headers = {"Authorization": f"Bearer {alice}"}
    resp = await client.post("/api/projects", json={"name": "notes-proj"}, headers=headers)
    project_id = resp.json()["id"]

    async with get_sessionmaker()() as session:
        p1 = await add_paper(
            session,
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
    return project_id, headers, ids


async def test_notes_crud(client):
    project_id, alice, ids = await _setup(client)

    resp = await client.post(
        f"/api/papers/{ids['p1']}/notes", json={"content": "重点：方法部分"}, headers=alice
    )
    assert resp.status_code == 201, resp.text
    note = resp.json()
    assert note["paper_id"] == ids["p1"]
    assert note["author_name"] == "Local"  # display_name 优先
    assert "project_id" not in note  # P5b：笔记不再挂项目
    note_id = note["id"]

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
    project_id, alice, ids = await _setup(client)
    # alice 开第二个课题，并把 p1 也收进第二个课题的方向库
    resp = await client.post("/api/projects", json={"name": "notes-proj-2"}, headers=alice)
    project2_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        from app.models.library_direction import LibraryPaper
        from tests.conftest import ensure_project_library

        library2 = await ensure_project_library(session, uuid.UUID(project2_id))
        session.add(
            LibraryPaper(library_id=library2.id, paper_id=uuid.UUID(ids["p1"]), status="included")
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


async def test_project_notebook_pagination_and_search(client):
    project_id, alice, ids = await _setup(client)
    for i in range(3):
        await client.post(
            f"/api/papers/{ids['p1']}/notes", json={"content": f"note-{i} 关于对齐"}, headers=alice
        )
    await client.post(
        f"/api/papers/{ids['p2']}/notes", json={"content": "另一篇的量子笔记"}, headers=alice
    )

    resp = await client.get(f"/api/projects/{project_id}/notes?size=2&page=1", headers=alice)
    body = resp.json()
    assert body["total"] == 4 and len(body["items"]) == 2
    assert body["items"][0]["content"] == "另一篇的量子笔记"  # created_at 倒序
    assert body["items"][0]["paper_title"] == "Second Paper"

    # q 内容搜索 + paper_id 过滤
    resp = await client.get(f"/api/projects/{project_id}/notes?q=量子", headers=alice)
    assert resp.json()["total"] == 1
    resp = await client.get(f"/api/projects/{project_id}/notes?paper_id={ids['p1']}", headers=alice)
    assert resp.json()["total"] == 3


async def test_keyword_search_includes_note_hits(client):
    project_id, alice, ids = await _setup(client)
    await client.post(
        f"/api/papers/{ids['p2']}/notes", json={"content": "提到了量子退火算法"}, headers=alice
    )
    resp = await client.get(
        f"/api/projects/{project_id}/search?q=量子退火&mode=keyword", headers=alice
    )
    titles = [p["title"] for p in resp.json()["papers"]]
    assert titles == ["Second Paper"]  # 仅笔记命中也返回


async def test_obsidian_export_includes_notes_section(client):
    project_id, alice, ids = await _setup(client)
    await client.post(
        f"/api/papers/{ids['p1']}/notes", json={"content": "导出验证用笔记"}, headers=alice
    )

    resp = await client.get(f"/api/projects/{project_id}/export/obsidian", headers=alice)
    assert resp.status_code == 200
    zf = zipfile.ZipFile(io.BytesIO(resp.content))
    paper_md = zf.read("papers/agent-planning-with-rl.md").decode("utf-8")
    assert "## 笔记" in paper_md
    assert "> **Local** (" in paper_md  # > **{author_name}** ({YYYY-MM-DD})
    assert "导出验证用笔记" in paper_md
    # 无笔记的论文页不加小节
    second_md = zf.read("papers/second-paper.md").decode("utf-8")
    assert "## 笔记" not in second_md
