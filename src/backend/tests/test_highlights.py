"""PDF 划线标注：CRUD + 颜色规整 + 排序 + 权限（P5b 起同笔记：仅作者本人可见）。"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.user import User
from tests.conftest import add_paper, register_and_login

RECT = {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.12}


async def _setup(client):
    """alice 建项目 + 一篇论文，bob 为项目成员。"""
    alice = await register_and_login(client)
    headers = {"Authorization": f"Bearer {alice}"}
    resp = await client.post("/api/projects", json={"name": "hl-proj"}, headers=headers)
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
            project_id=uuid.UUID(project_id), title="Attention Is All You Need", status="fetched"
        )
        session.add(p1)
        await session.commit()
        pid = str(p1.id)
    return project_id, headers, {"Authorization": f"Bearer {bob}"}, pid


async def test_highlight_crud_and_ordering(client):
    project_id, alice, bob, pid = await _setup(client)

    # 建划线（第 3 页）
    resp = await client.post(
        f"/api/papers/{pid}/highlights",
        json={"page": 3, "rects": [RECT], "selected_text": "self-attention", "color": "green"},
        headers=alice,
    )
    assert resp.status_code == 201, resp.text
    hl = resp.json()
    assert hl["paper_id"] == pid
    assert "project_id" not in hl  # P5b：划线不再挂项目
    assert hl["page"] == 3 and hl["color"] == "green" and hl["note"] is None
    assert hl["style"] == "highlight"  # 默认样式
    assert hl["author_name"] == "Alice"
    assert hl["rects"] == [RECT]
    hl_id = hl["id"]

    # alice 第 1 页再建一条；bob 也建一条（仅本人可见）
    await client.post(
        f"/api/papers/{pid}/highlights",
        json={"page": 1, "rects": [RECT], "selected_text": "encoder"},
        headers=alice,
    )
    await client.post(
        f"/api/papers/{pid}/highlights",
        json={"page": 2, "rects": [RECT], "selected_text": "bob 的划线"},
        headers=bob,
    )
    # 只看到自己的，按页码升序
    resp = await client.get(f"/api/papers/{pid}/highlights", headers=alice)
    rows = resp.json()
    assert [r["page"] for r in rows] == [1, 3]
    assert rows[0]["color"] == "yellow"  # 默认色
    resp = await client.get(f"/api/papers/{pid}/highlights", headers=bob)
    assert [r["selected_text"] for r in resp.json()] == ["bob 的划线"]

    # 作者改颜色 + 加批注
    resp = await client.patch(
        f"/api/highlights/{hl_id}", json={"color": "blue", "note": "核心机制"}, headers=alice
    )
    assert resp.status_code == 200
    assert resp.json()["color"] == "blue" and resp.json()["note"] == "核心机制"

    # 只传 note 不动 color
    resp = await client.patch(f"/api/highlights/{hl_id}", json={"note": "改了批注"}, headers=alice)
    assert resp.json()["color"] == "blue" and resp.json()["note"] == "改了批注"

    # 删除
    resp = await client.delete(f"/api/highlights/{hl_id}", headers=alice)
    assert resp.status_code == 204
    resp = await client.get(f"/api/papers/{pid}/highlights", headers=alice)
    assert [r["page"] for r in resp.json()] == [1]


async def test_highlight_style(client):
    _, alice, _, pid = await _setup(client)
    # 默认样式 highlight
    resp = await client.post(
        f"/api/papers/{pid}/highlights",
        json={"page": 1, "rects": [RECT], "selected_text": "a"},
        headers=alice,
    )
    assert resp.json()["style"] == "highlight"
    # 建波浪线，再改成下划线
    resp = await client.post(
        f"/api/papers/{pid}/highlights",
        json={"page": 1, "rects": [RECT], "selected_text": "b", "style": "wave"},
        headers=alice,
    )
    assert resp.json()["style"] == "wave"
    wid = resp.json()["id"]
    resp = await client.patch(f"/api/highlights/{wid}", json={"style": "underline"}, headers=alice)
    assert resp.json()["style"] == "underline"
    # 非法样式规整为 highlight
    resp = await client.post(
        f"/api/papers/{pid}/highlights",
        json={"page": 1, "rects": [RECT], "selected_text": "c", "style": "bogus"},
        headers=alice,
    )
    assert resp.json()["style"] == "highlight"


async def test_highlight_color_coerced_and_validation(client):
    _, alice, _, pid = await _setup(client)

    # 非法颜色 → 规整为 yellow
    resp = await client.post(
        f"/api/papers/{pid}/highlights",
        json={"page": 1, "rects": [RECT], "selected_text": "x", "color": "chartreuse"},
        headers=alice,
    )
    assert resp.status_code == 201
    assert resp.json()["color"] == "yellow"

    # 空 rects → 422
    resp = await client.post(
        f"/api/papers/{pid}/highlights",
        json={"page": 1, "rects": [], "selected_text": "x"},
        headers=alice,
    )
    assert resp.status_code == 422

    # 坐标越界 → 422
    resp = await client.post(
        f"/api/papers/{pid}/highlights",
        json={"page": 1, "rects": [{"x0": 0, "y0": 0, "x1": 1.4, "y1": 0.2}], "selected_text": "x"},
        headers=alice,
    )
    assert resp.status_code == 422


async def test_highlight_permissions(client):
    project_id, alice, bob, pid = await _setup(client)
    resp = await client.post(
        f"/api/papers/{pid}/highlights",
        json={"page": 1, "rects": [RECT], "selected_text": "alice 的划线"},
        headers=alice,
    )
    hl_id = resp.json()["id"]

    # 非作者成员改/删 → 404（P5b 起他人划线不可见，视为不存在）
    assert (
        await client.patch(f"/api/highlights/{hl_id}", json={"color": "pink"}, headers=bob)
    ).status_code == 404
    assert (await client.delete(f"/api/highlights/{hl_id}", headers=bob)).status_code == 404

    # 非项目成员：论文可读（P5c）→ 划线列表 200 但只见本人（空）；他人划线仍不可改
    mallory = await register_and_login(client, email="mallory@example.com")
    outsider = {"Authorization": f"Bearer {mallory}"}
    resp = await client.get(f"/api/papers/{pid}/highlights", headers=outsider)
    assert resp.status_code == 200 and resp.json() == []
    assert (
        await client.patch(f"/api/highlights/{hl_id}", json={"color": "pink"}, headers=outsider)
    ).status_code == 404

    # 平台 admin（bob 提权）可删非本人划线
    async with get_sessionmaker()() as session:
        user = (
            await session.execute(select(User).where(User.email == "bob@example.com"))
        ).scalar_one()
        user.role = "admin"
        await session.commit()
    assert (await client.delete(f"/api/highlights/{hl_id}", headers=bob)).status_code == 204
