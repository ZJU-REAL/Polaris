"""项目 API 增量：detail 形状 / PATCH 权限（成员机制已随 #625 移除）。"""

from tests.conftest import register_and_login


async def test_detail_shape_owner_only(client):
    token = await register_and_login(client, email="owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "mine"}, headers=headers)
    project_id = resp.json()["id"]

    resp = await client.get(f"/api/projects/{project_id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "mine"
    assert "owner_id" in body
    assert "members" not in body  # 成员机制已移除（#625）


async def test_patch_project(client):
    owner_token = await register_and_login(client, email="owner2@example.com")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    resp = await client.post("/api/projects", json={"name": "patch-me"}, headers=owner_headers)
    project_id = resp.json()["id"]

    # owner 可改 name/statement/status
    resp = await client.patch(
        f"/api/projects/{project_id}",
        json={"name": "改名", "statement": "方向", "status": "archived"},
        headers=owner_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "改名"
    assert body["statement"] == "方向"
    assert body["status"] == "archived"
