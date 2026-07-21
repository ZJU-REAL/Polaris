"""管理员用户管理 CRUD：新建 / 编辑（含用户名、改密）/ 删除 / 批量删除 / 权限守卫。"""

import uuid

from tests.conftest import register_and_login


async def _me_id(client, token) -> str:
    return (await client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})).json()[
        "id"
    ]


async def test_admin_create_user_and_login(client):
    admin = await register_and_login(client)  # 首个=admin
    ah = {"Authorization": f"Bearer {admin}"}
    resp = await client.post(
        "/api/admin/users",
        json={
            "email": "new@example.com",
            "password": "str0ng-pass",
            "display_name": "New User",
            "username": "new_user",
            "role": "member",
            "llm_access": "chat_only",
            "token_quota": 1000,
        },
        headers=ah,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["username"] == "new_user"
    assert body["role"] == "member"
    assert body["llm_access"] == "chat_only"
    assert body["token_quota"] == 1000
    # 新用户能登录（邮箱或用户名）
    login = await client.post(
        "/api/auth/jwt/login", data={"username": "new_user", "password": "str0ng-pass"}
    )
    assert login.status_code == 200


async def test_admin_create_duplicate_username_and_email(client):
    admin = await register_and_login(client)
    ah = {"Authorization": f"Bearer {admin}"}
    base = {"password": "str0ng-pass", "display_name": "X"}
    r1 = await client.post(
        "/api/admin/users", json={**base, "email": "a@example.com", "username": "dupe"}, headers=ah
    )
    assert r1.status_code == 201
    # 用户名重复 → 409
    r2 = await client.post(
        "/api/admin/users", json={**base, "email": "b@example.com", "username": "dupe"}, headers=ah
    )
    assert r2.status_code == 409
    assert r2.json()["detail"] == "USERNAME_TAKEN"
    # 邮箱重复 → 400
    r3 = await client.post(
        "/api/admin/users", json={**base, "email": "a@example.com", "username": "other"}, headers=ah
    )
    assert r3.status_code == 400
    assert r3.json()["detail"] == "EMAIL_TAKEN"


async def test_admin_edit_username_and_reset_password(client):
    admin = await register_and_login(client)
    member = await register_and_login(client, email="m@example.com")
    ah = {"Authorization": f"Bearer {admin}"}
    mid = await _me_id(client, member)
    # 改用户名 + 改姓名 + 重置密码
    r = await client.patch(
        f"/api/admin/users/{mid}",
        json={"username": "renamed", "display_name": "Renamed", "password": "new-str0ng-pass"},
        headers=ah,
    )
    assert r.status_code == 200, r.text
    assert r.json()["username"] == "renamed"
    assert r.json()["display_name"] == "Renamed"
    # 新密码可登录，旧密码不行
    assert (
        await client.post(
            "/api/auth/jwt/login", data={"username": "renamed", "password": "new-str0ng-pass"}
        )
    ).status_code == 200
    assert (
        await client.post(
            "/api/auth/jwt/login", data={"username": "renamed", "password": "str0ng-password"}
        )
    ).status_code == 400


async def test_admin_edit_username_conflict(client):
    admin = await register_and_login(client, email="admin@example.com", username="adminu")
    await register_and_login(client, email="u1@example.com", username="taken_name")
    u2 = await register_and_login(client, email="u2@example.com", username="u2name")
    ah = {"Authorization": f"Bearer {admin}"}
    u2id = await _me_id(client, u2)
    r = await client.patch(
        f"/api/admin/users/{u2id}", json={"username": "taken_name"}, headers=ah
    )
    assert r.status_code == 409


async def test_admin_delete_user_and_guard_self(client):
    admin = await register_and_login(client)
    member = await register_and_login(client, email="m@example.com")
    ah = {"Authorization": f"Bearer {admin}"}
    mid = await _me_id(client, member)
    aid = await _me_id(client, admin)
    # 删自己 → 400
    assert (await client.delete(f"/api/admin/users/{aid}", headers=ah)).status_code == 400
    # 删成员 → 204
    assert (await client.delete(f"/api/admin/users/{mid}", headers=ah)).status_code == 204
    # 列表里没了
    users = (await client.get("/api/admin/users", headers=ah)).json()
    assert all(u["id"] != mid for u in users)
    # 删不存在 → 404
    assert (
        await client.delete(f"/api/admin/users/{uuid.uuid4()}", headers=ah)
    ).status_code == 404


async def test_admin_batch_delete_skips_self(client):
    admin = await register_and_login(client)
    m1 = await register_and_login(client, email="m1@example.com")
    m2 = await register_and_login(client, email="m2@example.com")
    ah = {"Authorization": f"Bearer {admin}"}
    aid = await _me_id(client, admin)
    m1id = await _me_id(client, m1)
    m2id = await _me_id(client, m2)
    r = await client.post(
        "/api/admin/users/batch-delete", json={"user_ids": [aid, m1id, m2id]}, headers=ah
    )
    assert r.status_code == 200
    assert r.json()["deleted"] == 2  # 跳过自己
    users = (await client.get("/api/admin/users", headers=ah)).json()
    assert [u["id"] for u in users] == [aid]


async def test_non_admin_cannot_manage(client):
    await register_and_login(client)  # admin 占位
    member = await register_and_login(client, email="m@example.com")
    mh = {"Authorization": f"Bearer {member}"}
    assert (
        await client.post(
            "/api/admin/users",
            json={"email": "x@example.com", "password": "str0ng-pass", "display_name": "X", "username": "xuser"},
            headers=mh,
        )
    ).status_code == 403
    assert (
        await client.delete(f"/api/admin/users/{uuid.uuid4()}", headers=mh)
    ).status_code == 403
