"""Voyage API 契约测试（docs/api-m1.md §3）+ SSE 冒烟。"""

import json

from tests.conftest import register_and_login


async def _create_project(client, headers, name="proj"):
    resp = await client.post("/api/projects", json={"name": name}, headers=headers)
    assert resp.status_code == 201
    return resp.json()["id"]


async def test_voyages_require_auth(client):
    resp = await client.get("/api/voyages")
    assert resp.status_code == 401


async def test_create_list_detail_cancel(client, queue_stub):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _create_project(client, headers)

    resp = await client.post(
        "/api/voyages",
        json={"kind": "demo", "project_id": project_id, "goal": "目标 A"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    voyage = resp.json()
    for key in (
        "id",
        "kind",
        "goal",
        "status",
        "plan",
        "cursor",
        "budget",
        "usage",
        "project_id",
        "created_by",
        "created_at",
        "updated_at",
    ):
        assert key in voyage
    assert voyage["status"] == "planning"
    assert voyage["cursor"] == 0
    assert queue_stub.jobs == [("run_voyage", (voyage["id"],), {})]

    # list（含 project 过滤）
    resp = await client.get("/api/voyages", headers=headers)
    assert [v["id"] for v in resp.json()] == [voyage["id"]]
    resp = await client.get(f"/api/voyages?project_id={project_id}", headers=headers)
    assert len(resp.json()) == 1

    # detail 含 steps
    resp = await client.get(f"/api/voyages/{voyage['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["steps"] == []

    # cancel → cancelled；重复 cancel → 409
    resp = await client.post(f"/api/voyages/{voyage['id']}/cancel", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    resp = await client.post(f"/api/voyages/{voyage['id']}/cancel", headers=headers)
    assert resp.status_code == 409


async def test_voyage_invalid_kind_and_membership(client, queue_stub):
    token_a = await register_and_login(client, email="a@example.com")
    headers_a = {"Authorization": f"Bearer {token_a}"}
    project_id = await _create_project(client, headers_a)

    # 未知 kind → 422
    resp = await client.post(
        "/api/voyages",
        json={"kind": "nope", "project_id": project_id, "goal": "x"},
        headers=headers_a,
    )
    assert resp.status_code == 422

    resp = await client.post(
        "/api/voyages",
        json={"kind": "demo", "project_id": project_id, "goal": "x"},
        headers=headers_a,
    )
    voyage_id = resp.json()["id"]

    # 非成员：创建 404 / 详情 404 / 列表为空
    token_b = await register_and_login(client, email="b@example.com")
    headers_b = {"Authorization": f"Bearer {token_b}"}
    resp = await client.post(
        "/api/voyages",
        json={"kind": "demo", "project_id": project_id, "goal": "y"},
        headers=headers_b,
    )
    assert resp.status_code == 404
    resp = await client.get(f"/api/voyages/{voyage_id}", headers=headers_b)
    assert resp.status_code == 404
    resp = await client.get("/api/voyages", headers=headers_b)
    assert resp.json() == []


async def test_voyage_events_sse_smoke(client, queue_stub, fake_redis):
    """SSE 冒烟：拿到事件流响应头 + 补发的当前状态事件。

    注：httpx ASGITransport 会缓冲整个 ASGI 响应，无法对无界流做真流式断言；
    这里用已取消（终态）的航程 —— 端点补发状态帧后即收流。
    """
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    project_id = await _create_project(client, headers)
    resp = await client.post(
        "/api/voyages",
        json={"kind": "demo", "project_id": project_id, "goal": "SSE 冒烟"},
        headers=headers,
    )
    voyage_id = resp.json()["id"]
    resp = await client.post(f"/api/voyages/{voyage_id}/cancel", headers=headers)
    assert resp.status_code == 200

    async with client.stream("GET", f"/api/voyages/{voyage_id}/events", headers=headers) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        lines = resp.aiter_lines()
        assert await anext(lines) == "event: status"
        data_line = await anext(lines)
        assert data_line.startswith("data: ")
        payload = json.loads(data_line[len("data: ") :])
        assert payload == {"status": "cancelled", "cursor": 0}

    # 非成员拿不到事件流
    token_b = await register_and_login(client, email="sse-b@example.com")
    resp = await client.get(
        f"/api/voyages/{voyage_id}/events", headers={"Authorization": f"Bearer {token_b}"}
    )
    assert resp.status_code == 404
