"""相关研究书架 + 我的文献库 回收站（软删）：移出/召回/彻底删除/清空、复活、孤儿回收。"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.library import UserLibraryEntry
from app.models.paper import Paper
from app.models.topic_shelf import TopicPaper
from app.services import papers as papers_service
from tests.conftest import add_paper, register_and_login


async def _setup(client, *, name="trash-proj", email="alice@example.com"):
    token = await register_and_login(client, email=email)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], headers


async def _seed_paper(project_id, **fields):
    async with get_sessionmaker()() as session:
        paper = await add_paper(session, project_id=uuid.UUID(project_id), **fields)
        await session.commit()
        return str(paper.id)


async def _shelf(client, headers, project_id, **params):
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    resp = await client.get(f"/api/projects/{project_id}/shelf?{qs}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- 任务 A：课题相关研究书架 ----


async def test_shelf_trash_restore_purge_and_empty(client):
    project_id, headers = await _setup(client)
    p1 = await _seed_paper(project_id, title="Trashable One", status="scored")
    p2 = await _seed_paper(project_id, title="Keeper Two", status="scored")
    for pid in (p1, p2):
        resp = await client.post(
            f"/api/projects/{project_id}/shelf", json={"paper_id": pid}, headers=headers
        )
        assert resp.status_code == 201, resp.text

    # 移出 = 软删：默认列表看不到，回收站看得到
    resp = await client.delete(f"/api/projects/{project_id}/shelf/{p1}", headers=headers)
    assert resp.status_code == 204
    page = await _shelf(client, headers, project_id)
    assert [i["paper_id"] for i in page["items"]] == [p2]
    assert page["total"] == 1
    trash = await _shelf(client, headers, project_id, trashed="true")
    assert [i["paper_id"] for i in trash["items"]] == [p1]
    assert trash["items"][0]["trashed_at"] is not None
    # ids 端点（前端「已入架」勾选态）不含回收站条目
    resp = await client.get(f"/api/projects/{project_id}/shelf/ids", headers=headers)
    assert resp.json()["paper_ids"] == [p2]
    # 已在回收站里，再删一次 → 404
    resp = await client.delete(f"/api/projects/{project_id}/shelf/{p1}", headers=headers)
    assert resp.status_code == 404

    # 召回：回到默认列表，回收站清空
    resp = await client.post(
        f"/api/projects/{project_id}/shelf/{p1}/restore", headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["trashed_at"] is None
    page = await _shelf(client, headers, project_id)
    assert sorted(i["paper_id"] for i in page["items"]) == sorted([p1, p2])
    assert (await _shelf(client, headers, project_id, trashed="true"))["total"] == 0
    # 不在回收站里的条目召回 → 404
    resp = await client.post(
        f"/api/projects/{project_id}/shelf/{p1}/restore", headers=headers
    )
    assert resp.status_code == 404

    # 彻底删除：两边都没有
    resp = await client.delete(
        f"/api/projects/{project_id}/shelf/{p1}?hard=true", headers=headers
    )
    assert resp.status_code == 204
    assert [i["paper_id"] for i in (await _shelf(client, headers, project_id))["items"]] == [p2]
    assert (await _shelf(client, headers, project_id, trashed="true"))["total"] == 0
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(TopicPaper).where(TopicPaper.paper_id == uuid.UUID(p1))
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    # 清空回收站：只删软删行，在架的不动
    resp = await client.delete(f"/api/projects/{project_id}/shelf/{p2}", headers=headers)
    assert resp.status_code == 204
    p3 = await _seed_paper(project_id, title="Still Shelved", status="scored")
    await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": p3}, headers=headers
    )
    resp = await client.post(f"/api/projects/{project_id}/shelf/trash/empty", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 1}
    assert (await _shelf(client, headers, project_id, trashed="true"))["total"] == 0
    assert [i["paper_id"] for i in (await _shelf(client, headers, project_id))["items"]] == [p3]


async def test_shelf_readd_after_trash_revives_row(client):
    """软删后重新入架必须复活旧行——插新行会撞 uq_topic_papers_topic_paper 唯一键。"""
    project_id, headers = await _setup(client, name="revive-proj")
    resp = await client.post("/api/projects", json={"name": "revive-other"}, headers=headers)
    other_id = resp.json()["id"]
    # 论文最初只在本课题库里（入架时来源库 = 本课题库）
    paper_id = await _seed_paper(
        project_id, title="Revivable Paper", status="compiled", wiki_content="# 库版解读"
    )

    resp = await client.post(
        f"/api/projects/{project_id}/shelf",
        json={"paper_id": paper_id, "note": "初版备注"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    first_source = resp.json()["source_library_id"]
    assert first_source is not None

    resp = await client.delete(f"/api/projects/{project_id}/shelf/{paper_id}", headers=headers)
    assert resp.status_code == 204
    # 同时把个人库条目也退成未收藏（模拟用户两边都清掉）
    resp = await client.get(f"/api/me/library/state?paper_id={paper_id}", headers=headers)
    entry_id = resp.json()["entry_id"]
    await client.delete(f"/api/me/library/{entry_id}?mode=unsave", headers=headers)

    # 重新入架：不撞唯一键（500），复活同一行并更新 note
    resp = await client.post(
        f"/api/projects/{project_id}/shelf",
        json={"paper_id": paper_id, "note": "复活后备注"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["note"] == "复活后备注"
    assert body["trashed_at"] is None
    assert body["source_library_id"] == first_source  # 来源库按当下重取，仍是本课题库

    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(TopicPaper).where(TopicPaper.paper_id == uuid.UUID(paper_id))
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1  # 复活而非插新行
        assert rows[0].trashed_at is None and rows[0].trashed_by is None
        assert rows[0].note == "复活后备注"
        # 入架必入个人库：个人库条目一并回到收藏、出回收站
        entry = (
            await session.execute(
                select(UserLibraryEntry).where(
                    UserLibraryEntry.last_paper_id == uuid.UUID(paper_id)
                )
            )
        ).scalar_one()
        assert entry.saved is True and entry.trashed_at is None

    page = await _shelf(client, headers, project_id)
    assert [i["paper_id"] for i in page["items"]] == [paper_id]
    assert (await _shelf(client, headers, project_id, trashed="true"))["total"] == 0
    # 另一个课题的书架不受影响（唯一键是 topic_id × paper_id）
    assert (await _shelf(client, headers, other_id))["total"] == 0


async def test_shelf_trash_requires_project_membership(client):
    project_id, headers = await _setup(client, name="trash-authz")
    paper_id = await _seed_paper(project_id, title="Members Only", status="scored")
    await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": paper_id}, headers=headers
    )
    outsider = await register_and_login(client, email="trash-outsider@example.com")
    outsider_headers = {"Authorization": f"Bearer {outsider}"}

    for method, url in (
        ("get", f"/api/projects/{project_id}/shelf?trashed=true"),
        ("post", f"/api/projects/{project_id}/shelf/{paper_id}/restore"),
        ("post", f"/api/projects/{project_id}/shelf/trash/empty"),
        ("delete", f"/api/projects/{project_id}/shelf/{paper_id}?hard=true"),
    ):
        resp = await getattr(client, method)(url, headers=outsider_headers)
        assert resp.status_code == 404, (method, url, resp.text)


# ---- 任务 B：我的文献库 ----


async def _library(client, headers, tab):
    resp = await client.get(f"/api/me/library?tab={tab}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_library_unsave_goes_to_trash_and_restore(client):
    project_id, headers = await _setup(client, name="lib-trash")
    paper_id = await _seed_paper(project_id, title="Personal Paper", status="included")

    # 浏览 + 收藏
    await client.post("/api/me/library/visits", json={"paper_id": paper_id}, headers=headers)
    resp = await client.post("/api/me/library", json={"paper_id": paper_id}, headers=headers)
    assert resp.status_code == 201, resp.text
    entry_id = resp.json()["id"]
    assert resp.json()["trashed_at"] is None

    # 取消收藏 → 回收站；收藏 / 浏览记录都不含
    resp = await client.delete(f"/api/me/library/{entry_id}?mode=unsave", headers=headers)
    assert resp.status_code == 204
    assert (await _library(client, headers, "saved"))["total"] == 0
    assert (await _library(client, headers, "history"))["total"] == 0
    trash = await _library(client, headers, "trash")
    assert trash["total"] == 1
    assert trash["items"][0]["id"] == entry_id
    assert trash["items"][0]["trashed_at"] is not None

    # 召回：回到收藏，回收站清空
    resp = await client.post(f"/api/me/library/{entry_id}/restore", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["saved"] is True
    assert resp.json()["trashed_at"] is None
    assert (await _library(client, headers, "saved"))["total"] == 1
    assert (await _library(client, headers, "history"))["total"] == 1
    assert (await _library(client, headers, "trash"))["total"] == 0

    # 彻底删除
    await client.delete(f"/api/me/library/{entry_id}?mode=unsave", headers=headers)
    resp = await client.delete(f"/api/me/library/{entry_id}?mode=purge", headers=headers)
    assert resp.status_code == 204
    assert (await _library(client, headers, "trash"))["total"] == 0
    resp = await client.get(f"/api/me/library/{entry_id}", headers=headers)
    assert resp.status_code == 404


async def test_library_empty_trash_keeps_live_entries(client):
    project_id, headers = await _setup(client, name="lib-empty-trash")
    p1 = await _seed_paper(project_id, title="Trash Me", status="included")
    p2 = await _seed_paper(project_id, title="Keep Me", status="included")
    ids = {}
    for pid in (p1, p2):
        await client.post("/api/me/library/visits", json={"paper_id": pid}, headers=headers)
        resp = await client.post("/api/me/library", json={"paper_id": pid}, headers=headers)
        ids[pid] = resp.json()["id"]
    await client.delete(f"/api/me/library/{ids[p1]}?mode=unsave", headers=headers)

    resp = await client.post("/api/me/library/trash/empty", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 1}
    assert (await _library(client, headers, "trash"))["total"] == 0
    saved = await _library(client, headers, "saved")
    assert [i["id"] for i in saved["items"]] == [ids[p2]]


async def test_clear_history_keeps_trash(client):
    """清空浏览记录不该顺手清空回收站（旧实现 delete WHERE saved=false 会误删）。"""
    project_id, headers = await _setup(client, name="lib-clear-history")
    p_trash = await _seed_paper(project_id, title="In Trash", status="included")
    p_hist = await _seed_paper(project_id, title="Pure History", status="included")
    for pid in (p_trash, p_hist):
        await client.post("/api/me/library/visits", json={"paper_id": pid}, headers=headers)
    resp = await client.post("/api/me/library", json={"paper_id": p_trash}, headers=headers)
    trashed_id = resp.json()["id"]
    await client.delete(f"/api/me/library/{trashed_id}?mode=unsave", headers=headers)

    resp = await client.delete("/api/me/library/visits", headers=headers)
    assert resp.status_code == 204
    assert (await _library(client, headers, "history"))["total"] == 0  # 纯浏览条目被删
    trash = await _library(client, headers, "trash")
    assert [i["id"] for i in trash["items"]] == [trashed_id]  # 回收站原样保留
    assert trash["items"][0]["visit_count"] == 1  # 访问统计也没被清零


async def test_library_revisit_pulls_entry_out_of_trash(client):
    """重新打开阅读页 = 又要看它：条目出回收站，回到浏览记录。"""
    project_id, headers = await _setup(client, name="lib-revisit")
    paper_id = await _seed_paper(project_id, title="Reopened Paper", status="included")
    resp = await client.post(
        "/api/me/library/visits", json={"paper_id": paper_id}, headers=headers
    )
    entry_id = resp.json()["id"]
    await client.delete(f"/api/me/library/{entry_id}?mode=unsave", headers=headers)
    assert (await _library(client, headers, "trash"))["total"] == 1

    await client.post("/api/me/library/visits", json={"paper_id": paper_id}, headers=headers)
    assert (await _library(client, headers, "trash"))["total"] == 0
    assert (await _library(client, headers, "history"))["total"] == 1


# ---- 孤儿回收：回收站里的引用不给内容池论文续命 ----


async def test_trashed_shelf_row_does_not_keep_orphan_paper(client):
    """池中论文只被一条书架行引用；该行进回收站后不再算引用，gc 应回收本体。"""
    project_id, headers = await _setup(client, name="orphan-shelf")
    async with get_sessionmaker()() as session:
        paper = Paper(title="Pool Only Paper", source="manual")
        session.add(paper)
        await session.commit()
        paper_id = paper.id
    resp = await client.post(
        f"/api/projects/{project_id}/shelf", json={"paper_id": str(paper_id)}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    # 入架同时进了个人库（saved），先取消收藏，只剩书架这一处引用
    resp = await client.get(f"/api/me/library/state?paper_id={paper_id}", headers=headers)
    entry_id = resp.json()["entry_id"]
    resp = await client.delete(f"/api/me/library/{entry_id}?mode=unsave", headers=headers)
    assert resp.status_code == 204

    # 在架时：仍被引用，不回收
    async with get_sessionmaker()() as session:
        assert await papers_service.gc_orphan_papers(session, [paper_id]) == 0
        await session.commit()
        assert await session.get(Paper, paper_id) is not None

    resp = await client.delete(
        f"/api/projects/{project_id}/shelf/{paper_id}", headers=headers
    )
    assert resp.status_code == 204

    # 进回收站后：不再算引用，本体被回收（书架行随外键级联清理）
    async with get_sessionmaker()() as session:
        assert await papers_service.gc_orphan_papers(session, [paper_id]) == 1
        await session.commit()
        assert await session.get(Paper, paper_id) is None
