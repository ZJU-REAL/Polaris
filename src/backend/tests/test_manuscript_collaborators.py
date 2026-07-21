"""协作者管理 + 共享链接：用户搜索 / 加协作者 / 移除 / 分享链接 → 加入即获编辑权。"""

from tests.test_manuscripts import _create_manuscript, _setup_project


async def test_search_add_remove_collaborator(client):
    project_id, owner = await _setup_project(client, email="owner@example.com")
    resp = await _create_manuscript(client, owner, project_id)
    ms_id = resp.json()["id"]

    # 另一个平台用户（注册 + 拿到其 headers）
    _, bob = await _setup_project(client, email="bob.smith@example.com")

    # owner 搜到 bob
    resp = await client.get("/api/collaborators/search?q=bob.smith", headers=owner)
    assert resp.status_code == 200
    hits = resp.json()
    bob_id = next(h["id"] for h in hits if h["email"] == "bob.smith@example.com")

    # 初始只有 owner，且 is_owner=True
    resp = await client.get(f"/api/manuscripts/{ms_id}/collaborators", headers=owner)
    rows = resp.json()
    assert {c["email"] for c in rows} == {"owner@example.com"}
    assert rows[0]["is_owner"] is True

    # 加 bob 为协作者 → bob 能打开这篇稿件
    resp = await client.post(
        f"/api/manuscripts/{ms_id}/collaborators", json={"user_id": bob_id}, headers=owner
    )
    assert resp.status_code == 201
    assert {c["email"] for c in resp.json()} == {"owner@example.com", "bob.smith@example.com"}
    assert (await client.get(f"/api/manuscripts/{ms_id}", headers=bob)).status_code == 200

    # 移除 bob → 非成员看不到
    resp = await client.delete(f"/api/manuscripts/{ms_id}/collaborators/{bob_id}", headers=owner)
    assert resp.status_code == 204
    assert (await client.get(f"/api/manuscripts/{ms_id}", headers=bob)).status_code == 404


async def test_share_link_grants_edit_on_join(client):
    project_id, owner = await _setup_project(client, email="a@example.com")
    resp = await _create_manuscript(client, owner, project_id)
    ms_id = resp.json()["id"]

    resp = await client.post(f"/api/manuscripts/{ms_id}/share-link", json={}, headers=owner)
    assert resp.status_code == 201
    token = resp.json()["token"]
    assert resp.json()["join_path"] == f"/join/{token}"

    _, guest = await _setup_project(client, email="guest@example.com")
    assert (await client.get(f"/api/manuscripts/{ms_id}", headers=guest)).status_code == 404
    assert (await client.post(f"/api/invites/{token}/accept", headers=guest)).status_code == 200
    assert (await client.get(f"/api/manuscripts/{ms_id}", headers=guest)).status_code == 200


async def test_add_collaborator_requires_manage(client):
    project_id, owner = await _setup_project(client, email="mgr@example.com")
    resp = await _create_manuscript(client, owner, project_id)
    ms_id = resp.json()["id"]

    _, member = await _setup_project(client, email="member@example.com")
    mid = (await client.get("/api/collaborators/search?q=member@example", headers=owner)).json()[0][
        "id"
    ]
    await client.post(
        f"/api/manuscripts/{ms_id}/collaborators", json={"user_id": mid}, headers=owner
    )
    # member 非 owner/管理员 → 加人被拒
    resp = await client.post(
        f"/api/manuscripts/{ms_id}/collaborators", json={"user_id": mid}, headers=member
    )
    assert resp.status_code == 403
