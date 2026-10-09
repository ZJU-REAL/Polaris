"""PDF 划线标注：CRUD + 颜色规整 + 排序 + 权限（P5b 起同笔记：仅作者本人可见）。"""

import uuid

from app.core.db import get_sessionmaker
from tests.conftest import add_paper, register_and_login

RECT = {"x0": 0.1, "y0": 0.1, "x1": 0.5, "y1": 0.12}


async def _setup(client):
    """建项目 + 一篇论文。"""
    alice = await register_and_login(client)
    headers = {"Authorization": f"Bearer {alice}"}
    resp = await client.post("/api/projects", json={"name": "hl-proj"}, headers=headers)
    project_id = resp.json()["id"]

    async with get_sessionmaker()() as session:
        p1 = await add_paper(
            session,
            project_id=uuid.UUID(project_id),
            title="Attention Is All You Need",
            status="fetched",
        )
        session.add(p1)
        await session.commit()
        pid = str(p1.id)
    return project_id, headers, pid


async def test_highlight_crud_and_ordering(client):
    project_id, alice, pid = await _setup(client)

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
    assert hl["author_name"] == "Local"
    assert hl["rects"] == [RECT]
    hl_id = hl["id"]

    # 第 1 页再建一条
    await client.post(
        f"/api/papers/{pid}/highlights",
        json={"page": 1, "rects": [RECT], "selected_text": "encoder"},
        headers=alice,
    )
    # 按页码升序
    resp = await client.get(f"/api/papers/{pid}/highlights", headers=alice)
    rows = resp.json()
    assert [r["page"] for r in rows] == [1, 3]
    assert rows[0]["color"] == "yellow"  # 默认色

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
    _, alice, pid = await _setup(client)
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
    _, alice, pid = await _setup(client)

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
