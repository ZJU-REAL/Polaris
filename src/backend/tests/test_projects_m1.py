"""M1 项目 API 增量：detail 含 members / PATCH / 加成员。"""

from tests.conftest import register_and_login


async def test_detail_contains_members(client):
    token = await register_and_login(client, email="owner@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.post("/api/projects", json={"name": "with-members"}, headers=headers)
    project_id = resp.json()["id"]

    resp = await client.get(f"/api/projects/{project_id}", headers=headers)
    assert resp.status_code == 200
    members = resp.json()["members"]
    assert len(members) == 1
    assert members[0]["role"] == "owner"
    assert members[0]["email"] == "owner@example.com"


async def test_patch_project_permissions(client):
    # 首个用户是平台 admin；owner 用第二个用户，普通成员用第三个
    admin_token = await register_and_login(client, email="root@example.com")
    owner_token = await register_and_login(client, email="owner2@example.com")
    member_token = await register_and_login(client, email="member2@example.com")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    member_headers = {"Authorization": f"Bearer {member_token}"}
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

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

    # 加成员：按 email；未知邮箱 404
    resp = await client.post(
        f"/api/projects/{project_id}/members",
        json={"email": "member2@example.com", "role": "member"},
        headers=owner_headers,
    )
    assert resp.status_code == 204
    resp = await client.post(
        f"/api/projects/{project_id}/members",
        json={"email": "ghost@example.com", "role": "member"},
        headers=owner_headers,
    )
    assert resp.status_code == 404

    # 普通成员：能看 detail，但 PATCH / 加成员 403
    resp = await client.get(f"/api/projects/{project_id}", headers=member_headers)
    assert resp.status_code == 200
    emails = {m["email"] for m in resp.json()["members"]}
    assert emails == {"owner2@example.com", "member2@example.com"}
    resp = await client.patch(
        f"/api/projects/{project_id}", json={"name": "hack"}, headers=member_headers
    )
    assert resp.status_code == 403
    resp = await client.post(
        f"/api/projects/{project_id}/members",
        json={"email": "root@example.com"},
        headers=member_headers,
    )
    assert resp.status_code == 403

    # 非成员（即使 admin）看不到 → 404；加入后 admin 可管理
    resp = await client.patch(
        f"/api/projects/{project_id}", json={"name": "x"}, headers=admin_headers
    )
    assert resp.status_code == 404
    resp = await client.post(
        f"/api/projects/{project_id}/members",
        json={"email": "root@example.com", "role": "member"},
        headers=owner_headers,
    )
    assert resp.status_code == 204
    resp = await client.patch(
        f"/api/projects/{project_id}", json={"name": "admin 改"}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "admin 改"
