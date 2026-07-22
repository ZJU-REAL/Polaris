"""注册码：管理员生成 / 列表 / 停用；注册时核销（过期 / 次数 / 停用即失效）。"""

from tests.conftest import register_and_login


async def _admin_token(client) -> str:
    # 首个注册用户自动成为 admin
    return await register_and_login(client, email="admin@example.com")


async def _register(client, code: str, email: str, username: str):
    return await client.post(
        "/api/auth/register",
        json={
            "email": email,
            "password": "str0ng-password",
            "display_name": "New",
            "username": username,
            "invite_code": code,
        },
    )


async def test_admin_create_list_and_use_code(client):
    token = await _admin_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/admin/registration-codes",
        json={"note": "新生一批", "max_uses": 2},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    code = resp.json()["code"]
    assert code.startswith("POLARIS-")
    assert resp.json()["status"] == "active"
    assert resp.json()["used_count"] == 0

    # 列表可见
    resp = await client.get("/api/admin/registration-codes", headers=headers)
    assert resp.status_code == 200
    assert any(c["code"] == code for c in resp.json())

    # 用该码注册两次成功，用尽后第三次失败
    assert (await _register(client, code, "u1@example.com", "user_one")).status_code == 201
    assert (await _register(client, code, "u2@example.com", "user_two")).status_code == 201
    r3 = await _register(client, code, "u3@example.com", "user_three")
    assert r3.status_code == 403
    assert r3.json()["detail"] == "INVALID_INVITE_CODE"

    # 用尽后状态变 exhausted，used_count=2
    listed = {
        c["code"]: c
        for c in (await client.get("/api/admin/registration-codes", headers=headers)).json()
    }
    assert listed[code]["used_count"] == 2
    assert listed[code]["status"] == "exhausted"


async def test_revoked_code_rejected(client):
    token = await _admin_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    created = (await client.post("/api/admin/registration-codes", json={}, headers=headers)).json()
    code, code_id = created["code"], created["id"]

    resp = await client.delete(f"/api/admin/registration-codes/{code_id}", headers=headers)
    assert resp.status_code == 204

    r = await _register(client, code, "nope@example.com", "nope_user")
    assert r.status_code == 403


async def test_preset_directions_create_projects(client):
    token = await _admin_token(client)
    headers = {"Authorization": f"Bearer {token}"}

    resp = await client.post(
        "/api/admin/registration-codes",
        json={"preset_directions": ["  LLM 长程规划  ", "多模态检索", "LLM 长程规划", ""]},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    # 去空白 / 去空项 / 去重（保序）
    assert resp.json()["preset_directions"] == ["LLM 长程规划", "多模态检索"]
    code = resp.json()["code"]

    r = await _register(client, code, "invited@example.com", "invited_user")
    assert r.status_code == 201, r.text

    login = await client.post(
        "/api/auth/jwt/login",
        data={"username": "invited@example.com", "password": "str0ng-password"},
    )
    assert login.status_code == 200
    invited_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    projects = (await client.get("/api/projects", headers=invited_headers)).json()
    by_name = {p["name"]: p for p in projects}
    assert set(by_name) == {"LLM 长程规划", "多模态检索"}
    assert by_name["LLM 长程规划"]["definition"] == {"statement": "LLM 长程规划"}


async def test_code_without_directions_creates_no_projects(client):
    token = await _admin_token(client)
    headers = {"Authorization": f"Bearer {token}"}
    code = (
        await client.post("/api/admin/registration-codes", json={}, headers=headers)
    ).json()["code"]

    r = await _register(client, code, "plain@example.com", "plain_user")
    assert r.status_code == 201

    login = await client.post(
        "/api/auth/jwt/login",
        data={"username": "plain@example.com", "password": "str0ng-password"},
    )
    plain_headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/api/projects", headers=plain_headers)).json() == []


async def test_static_fallback_code_still_works(client):
    # 没建过任何 DB 码时，settings.invite_code（测试里 = test-invite）仍可注册（兜底）
    r = await _register(client, "test-invite", "fallback@example.com", "fallback_user")
    assert r.status_code == 201


async def test_non_admin_cannot_manage_codes(client):
    await _admin_token(client)  # 首个用户占掉 admin
    member = await register_and_login(client, email="member@example.com")
    headers = {"Authorization": f"Bearer {member}"}
    assert (
        await client.post("/api/admin/registration-codes", json={}, headers=headers)
    ).status_code == 403
    assert (await client.get("/api/admin/registration-codes", headers=headers)).status_code == 403
