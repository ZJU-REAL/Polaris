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
