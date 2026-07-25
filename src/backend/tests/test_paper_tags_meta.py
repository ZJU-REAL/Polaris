"""标签与个人状态（docs/api-lit.md §5）：整组覆盖、项目标签、my-meta、列表过滤与新字段。"""

import uuid

from app.core.db import get_sessionmaker
from tests.conftest import add_paper, register_and_login


async def _setup(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "tags-proj"}, headers=headers)
    project_id = resp.json()["id"]
    async with get_sessionmaker()() as session:
        p1 = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Paper One",
            status="included",
        )
        p2 = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Paper Two",
            status="compiled",
        )
        session.add_all([p1, p2])
        await session.commit()
        ids = {"p1": str(p1.id), "p2": str(p2.id)}
    return project_id, headers, ids


async def test_tags_put_overwrite_and_project_tags(client):
    project_id, headers, ids = await _setup(client)

    resp = await client.put(
        f"/api/papers/{ids['p1']}/tags", json={"names": ["方法", "评测", "方法"]}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["tags"] == ["方法", "评测"]  # 去重 + 排序

    await client.put(f"/api/papers/{ids['p2']}/tags", json={"names": ["方法"]}, headers=headers)
    resp = await client.get(f"/api/projects/{project_id}/tags", headers=headers)
    tags = {t["name"]: t["paper_count"] for t in resp.json()}
    assert tags == {"方法": 2, "评测": 1}

    # 整组覆盖：p1 只留「评测」；「方法」tag 记录保留但只挂 p2
    resp = await client.put(
        f"/api/papers/{ids['p1']}/tags", json={"names": ["评测"]}, headers=headers
    )
    assert resp.json()["tags"] == ["评测"]
    resp = await client.get(f"/api/projects/{project_id}/tags", headers=headers)
    tags = {t["name"]: t["paper_count"] for t in resp.json()}
    assert tags == {"方法": 1, "评测": 1}

    # 清空：p1 摘掉「评测」后它零引用，自动清理；「方法」仍挂 p2 保留
    resp = await client.put(f"/api/papers/{ids['p1']}/tags", json={"names": []}, headers=headers)
    assert resp.json()["tags"] == []
    resp = await client.get(f"/api/projects/{project_id}/tags", headers=headers)
    tags = {t["name"]: t["paper_count"] for t in resp.json()}
    assert tags == {"方法": 1}


async def test_orphan_tags_pruned_on_hard_delete_and_empty_trash(client):
    project_id, headers, ids = await _setup(client)
    await client.put(
        f"/api/papers/{ids['p1']}/tags", json={"names": ["独占", "共享"]}, headers=headers
    )
    await client.put(f"/api/papers/{ids['p2']}/tags", json={"names": ["共享"]}, headers=headers)

    async def tag_counts():
        resp = await client.get(f"/api/projects/{project_id}/tags", headers=headers)
        return {t["name"]: t["paper_count"] for t in resp.json()}

    # 软删（进回收站）不清理：回收站论文的引用也算数
    resp = await client.post(
        f"/api/projects/{project_id}/papers/batch-delete",
        json={"paper_ids": [ids["p1"]]},
        headers=headers,
    )
    assert resp.status_code == 200
    assert await tag_counts() == {"共享": 2, "独占": 1}

    # 硬删 p1：「独占」失去全部引用被清理，「共享」还挂 p2 保留
    resp = await client.post(
        f"/api/projects/{project_id}/papers/batch-delete",
        json={"paper_ids": [ids["p1"]], "hard": True},
        headers=headers,
    )
    assert resp.status_code == 200
    assert await tag_counts() == {"共享": 1}

    # p2 进回收站后清空回收站：「共享」也失去全部引用，一并清理
    await client.post(
        f"/api/projects/{project_id}/papers/batch-delete",
        json={"paper_ids": [ids["p2"]]},
        headers=headers,
    )
    assert await tag_counts() == {"共享": 1}
    resp = await client.post(f"/api/projects/{project_id}/trash/empty", headers=headers)
    assert resp.status_code == 200
    assert await tag_counts() == {}


async def test_orphan_tags_pruned_on_single_hard_delete(client):
    _project_id, headers, ids = await _setup(client)
    await client.put(f"/api/papers/{ids['p1']}/tags", json={"names": ["仅此一篇"]}, headers=headers)

    resp = await client.delete(f"/api/papers/{ids['p1']}", headers=headers)
    assert resp.status_code == 204

    resp = await client.get(f"/api/projects/{_project_id}/tags", headers=headers)
    assert resp.json() == []


async def test_my_meta_upsert_and_per_user_view(client):
    project_id, headers, ids = await _setup(client)

    resp = await client.put(
        f"/api/papers/{ids['p1']}/my-meta", json={"starred": True}, headers=headers
    )
    assert resp.status_code == 200
    assert resp.json() == {"starred": True, "reading_status": "unread"}

    # 部分更新：只改 reading_status，starred 保留
    resp = await client.put(
        f"/api/papers/{ids['p1']}/my-meta", json={"reading_status": "reading"}, headers=headers
    )
    assert resp.json() == {"starred": True, "reading_status": "reading"}

    # 非法阅读状态 → 422
    resp = await client.put(
        f"/api/papers/{ids['p1']}/my-meta", json={"reading_status": "done"}, headers=headers
    )
    assert resp.status_code == 422

    # 个人视角互不影响：另一成员看到默认值
    bob = await register_and_login(client, email="bob-meta@example.com")
    await client.post(
        f"/api/projects/{project_id}/members",
        json={"email": "bob-meta@example.com", "role": "member"},
        headers=headers,
    )
    resp = await client.get(f"/api/papers/{ids['p1']}", headers={"Authorization": f"Bearer {bob}"})
    body = resp.json()
    assert body["starred"] is False and body["reading_status"] == "unread"


async def test_papers_list_new_fields_and_filters(client):
    project_id, headers, ids = await _setup(client)
    await client.put(f"/api/papers/{ids['p1']}/tags", json={"names": ["核心"]}, headers=headers)
    await client.put(
        f"/api/papers/{ids['p1']}/my-meta",
        json={"starred": True, "reading_status": "read"},
        headers=headers,
    )
    await client.post(f"/api/papers/{ids['p1']}/notes", json={"content": "n1"}, headers=headers)
    await client.post(f"/api/papers/{ids['p1']}/notes", json={"content": "n2"}, headers=headers)

    resp = await client.get(f"/api/projects/{project_id}/papers", headers=headers)
    by_title = {p["title"]: p for p in resp.json()["items"]}
    p1, p2 = by_title["Paper One"], by_title["Paper Two"]
    assert p1["tags"] == ["核心"] and p1["starred"] is True
    assert p1["reading_status"] == "read" and p1["note_count"] == 2
    assert p2["tags"] == [] and p2["starred"] is False
    assert p2["reading_status"] == "unread" and p2["note_count"] == 0

    # tag / starred / reading_status 过滤
    resp = await client.get(f"/api/projects/{project_id}/papers?tag=核心", headers=headers)
    assert [p["title"] for p in resp.json()["items"]] == ["Paper One"]
    resp = await client.get(f"/api/projects/{project_id}/papers?starred=true", headers=headers)
    assert [p["title"] for p in resp.json()["items"]] == ["Paper One"]
    resp = await client.get(
        f"/api/projects/{project_id}/papers?reading_status=read", headers=headers
    )
    assert [p["title"] for p in resp.json()["items"]] == ["Paper One"]
    # 无 meta 记录的论文默认 unread，也要能被 unread 过滤出来
    resp = await client.get(
        f"/api/projects/{project_id}/papers?reading_status=unread", headers=headers
    )
    assert [p["title"] for p in resp.json()["items"]] == ["Paper Two"]


# ---- 个人标签：只有本人可见可改，与库标签互不影响 ----


async def test_my_tags_put_overwrite_clear_and_list(client):
    project_id, headers, ids = await _setup(client)

    resp = await client.put(
        f"/api/papers/{ids['p1']}/my-tags",
        json={"names": ["精读", "待复现", "精读"]},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"my_tags": ["待复现", "精读"]}  # 去重 + 排序

    # 详情 / 列表都带上 my_tags
    resp = await client.get(f"/api/papers/{ids['p1']}", headers=headers)
    assert resp.json()["my_tags"] == ["待复现", "精读"]
    resp = await client.get(f"/api/projects/{project_id}/papers", headers=headers)
    by_title = {p["title"]: p for p in resp.json()["items"]}
    assert by_title["Paper One"]["my_tags"] == ["待复现", "精读"]
    assert by_title["Paper Two"]["my_tags"] == []

    # 「我的所有标签」含标了几篇
    await client.put(f"/api/papers/{ids['p2']}/my-tags", json={"names": ["精读"]}, headers=headers)
    resp = await client.get("/api/me/paper-tags", headers=headers)
    assert resp.json() == [
        {"name": "待复现", "paper_count": 1},
        {"name": "精读", "paper_count": 2},
    ]

    # 整组覆盖：p1 只留「精读」
    resp = await client.put(
        f"/api/papers/{ids['p1']}/my-tags", json={"names": ["精读"]}, headers=headers
    )
    assert resp.json() == {"my_tags": ["精读"]}

    # 清空
    resp = await client.put(f"/api/papers/{ids['p1']}/my-tags", json={"names": []}, headers=headers)
    assert resp.json() == {"my_tags": []}
    resp = await client.get("/api/me/paper-tags", headers=headers)
    assert resp.json() == [{"name": "精读", "paper_count": 1}]  # 只剩 p2 那条


async def test_my_tags_are_per_user(client):
    project_id, headers, ids = await _setup(client)
    await client.put(f"/api/papers/{ids['p1']}/my-tags", json={"names": ["我的"]}, headers=headers)

    bob = await register_and_login(client, email="bob-mytags@example.com")
    bob_headers = {"Authorization": f"Bearer {bob}"}
    await client.post(
        f"/api/projects/{project_id}/members",
        json={"email": "bob-mytags@example.com", "role": "member"},
        headers=headers,
    )

    # 同一篇论文，看不到别人的个人标签
    resp = await client.get(f"/api/papers/{ids['p1']}", headers=bob_headers)
    assert resp.json()["my_tags"] == []
    assert (await client.get("/api/me/paper-tags", headers=bob_headers)).json() == []

    # 各打各的，互不覆盖
    await client.put(
        f"/api/papers/{ids['p1']}/my-tags", json={"names": ["他的"]}, headers=bob_headers
    )
    alice_view = await client.get(f"/api/papers/{ids['p1']}", headers=headers)
    bob_view = await client.get(f"/api/papers/{ids['p1']}", headers=bob_headers)
    assert alice_view.json()["my_tags"] == ["我的"]
    assert bob_view.json()["my_tags"] == ["他的"]


async def test_stranger_can_tag_paper_in_public_library(client):
    """公共库里的陌生人也能打个人标签（放行口径对齐 my-meta）。"""
    _project_id, headers, ids = await _setup(client)
    stranger = await register_and_login(client, email="stranger-mytags@example.com")
    stranger_headers = {"Authorization": f"Bearer {stranger}"}

    resp = await client.put(
        f"/api/papers/{ids['p1']}/my-tags", json={"names": ["路人的"]}, headers=stranger_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"my_tags": ["路人的"]}
    resp = await client.get(f"/api/papers/{ids['p1']}", headers=stranger_headers)
    assert resp.json()["my_tags"] == ["路人的"]
    # 库归属人看不到路人的个人标签
    assert (await client.get(f"/api/papers/{ids['p1']}", headers=headers)).json()["my_tags"] == []


async def test_my_tag_filter_and_independence_from_library_tags(client):
    project_id, headers, ids = await _setup(client)
    await client.put(f"/api/papers/{ids['p1']}/tags", json={"names": ["库标签"]}, headers=headers)
    await client.put(
        f"/api/papers/{ids['p1']}/my-tags", json={"names": ["个人标签"]}, headers=headers
    )
    await client.put(f"/api/papers/{ids['p2']}/my-tags", json={"names": ["别的"]}, headers=headers)

    # 按个人标签筛选
    resp = await client.get(f"/api/projects/{project_id}/papers?my_tag=个人标签", headers=headers)
    assert [p["title"] for p in resp.json()["items"]] == ["Paper One"]
    resp = await client.get(f"/api/projects/{project_id}/papers?my_tag=没人用过", headers=headers)
    assert resp.json()["items"] == []
    # 库标签筛选不认个人标签名
    resp = await client.get(f"/api/projects/{project_id}/papers?tag=个人标签", headers=headers)
    assert resp.json()["items"] == []

    # 两份清单互不串：库标签列表里没有个人标签，反之亦然
    resp = await client.get(f"/api/projects/{project_id}/tags", headers=headers)
    assert [t["name"] for t in resp.json()] == ["库标签"]
    resp = await client.get("/api/me/paper-tags", headers=headers)
    assert [t["name"] for t in resp.json()] == ["个人标签", "别的"]

    # 覆盖库标签不动个人标签，反之亦然
    await client.put(f"/api/papers/{ids['p1']}/tags", json={"names": []}, headers=headers)
    body = (await client.get(f"/api/papers/{ids['p1']}", headers=headers)).json()
    assert body["tags"] == [] and body["my_tags"] == ["个人标签"]
    await client.put(f"/api/papers/{ids['p1']}/my-tags", json={"names": []}, headers=headers)
    await client.put(f"/api/papers/{ids['p1']}/tags", json={"names": ["库标签"]}, headers=headers)
    body = (await client.get(f"/api/papers/{ids['p1']}", headers=headers)).json()
    assert body["tags"] == ["库标签"] and body["my_tags"] == []


async def test_library_tags_scoped_to_browsing_library(client):
    """一篇论文同时在 A、B 两个库时，在哪个库浏览就只看到哪个库打的库标签。"""
    from sqlalchemy import insert

    from app.models.library_direction import LibraryPaper
    from app.models.paper import PaperTag, paper_tag_links
    from tests.conftest import ensure_project_library

    token = await register_and_login(client, email="two-lib@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    a_id = (await client.post("/api/projects", json={"name": "库A"}, headers=headers)).json()["id"]
    b_id = (await client.post("/api/projects", json={"name": "库B"}, headers=headers)).json()["id"]

    async with get_sessionmaker()() as session:
        paper = await add_paper(
            session, project_id=uuid.UUID(a_id), title="Cross Library Paper", status="included"
        )
        lib_a = await ensure_project_library(session, uuid.UUID(a_id))
        lib_b = await ensure_project_library(session, uuid.UUID(b_id))
        session.add(LibraryPaper(library_id=lib_b.id, paper_id=paper.id, status="included"))
        for library_id, name in ((lib_a.id, "A库标签"), (lib_b.id, "B库标签")):
            tag = PaperTag(library_id=library_id, name=name)
            session.add(tag)
            await session.flush()
            await session.execute(insert(paper_tag_links).values(paper_id=paper.id, tag_id=tag.id))
        await session.commit()
        paper_id, lib_a_id, lib_b_id = str(paper.id), str(lib_a.id), str(lib_b.id)

    for library_id, expected in ((lib_a_id, "A库标签"), (lib_b_id, "B库标签")):
        resp = await client.get(f"/api/libraries/{library_id}/papers", headers=headers)
        assert resp.status_code == 200, resp.text
        assert [p["tags"] for p in resp.json()["items"]] == [[expected]]
        resp = await client.get(f"/api/libraries/{library_id}/papers/{paper_id}", headers=headers)
        assert resp.json()["tags"] == [expected]

    # 课题论文列表按该课题的关联库并集显示（这里各自只关联自己那个库）
    resp = await client.get(f"/api/projects/{a_id}/papers", headers=headers)
    assert [p["tags"] for p in resp.json()["items"]] == [["A库标签"]]
    resp = await client.get(f"/api/projects/{b_id}/papers", headers=headers)
    assert [p["tags"] for p in resp.json()["items"]] == [["B库标签"]]
