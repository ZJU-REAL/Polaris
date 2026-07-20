"""注册需姓名+唯一用户名；登录支持 邮箱或用户名。"""

from tests.conftest import INVITE_CODE


async def _register(client, **over):
    body = {
        "email": "neo@example.com",
        "password": "str0ng-password",
        "display_name": "Neo",
        "username": "neo",
        "invite_code": INVITE_CODE,
    }
    body.update(over)
    return await client.post("/api/auth/register", json=body)


async def _login(client, ident: str, password: str = "str0ng-password"):
    return await client.post(
        "/api/auth/jwt/login", data={"username": ident, "password": password}
    )


async def test_register_requires_name_and_username(client):
    # 缺姓名 → 422
    resp = await _register(client, display_name="")
    assert resp.status_code == 422
    # 缺用户名 → 422
    resp = await client.post(
        "/api/auth/register",
        json={
            "email": "a@example.com",
            "password": "str0ng-password",
            "display_name": "A",
            "invite_code": INVITE_CODE,
        },
    )
    assert resp.status_code == 422


async def test_username_format_enforced(client):
    for bad in ["ab", "UPPER", "has space", "with-dash", "toolong" * 6]:
        resp = await _register(client, email=f"x{len(bad)}@e.com", username=bad)
        assert resp.status_code == 422, f"{bad!r} should be rejected"


async def test_username_must_be_unique(client):
    resp = await _register(client)
    assert resp.status_code == 201, resp.text
    assert resp.json()["username"] == "neo"
    # 同名（不同邮箱）→ 400 USERNAME_TAKEN
    resp = await _register(client, email="other@example.com", username="neo")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "USERNAME_TAKEN"


async def test_login_by_email_or_username(client):
    resp = await _register(client)
    assert resp.status_code == 201

    # 邮箱登录
    resp = await _login(client, "neo@example.com")
    assert resp.status_code == 200 and resp.json()["access_token"]
    # 用户名登录
    resp = await _login(client, "neo")
    assert resp.status_code == 200 and resp.json()["access_token"]
    # 用户名大小写不敏感（存储为小写）
    resp = await _login(client, "NEO")
    assert resp.status_code == 200

    # 错误密码 → 400（fastapi-users BAD_CREDENTIALS）
    resp = await _login(client, "neo", password="wrong-pass")
    assert resp.status_code == 400
    # 不存在的标识 → 400
    resp = await _login(client, "ghost")
    assert resp.status_code == 400


async def test_me_exposes_username(client):
    assert (await _register(client)).status_code == 201
    token = (await _login(client, "neo")).json()["access_token"]
    me = await client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "neo"


async def test_username_change_once_then_locked(client):
    # 首个用户注册即拿到用户名，未锁定
    resp = await _register(client)
    assert resp.status_code == 201
    assert resp.json()["username_locked"] is False
    token = (await _login(client, "neo")).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}

    # 改一次成功并锁定
    resp = await client.patch("/api/users/me/username", json={"username": "trinity"}, headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "trinity"
    assert resp.json()["username_locked"] is True
    # 用新用户名可登录
    assert (await _login(client, "trinity")).status_code == 200

    # 第二次修改被拒
    resp = await client.patch("/api/users/me/username", json={"username": "morpheus"}, headers=h)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "USERNAME_LOCKED"


async def test_username_change_rejects_taken_and_bad_format(client):
    await _register(client)  # neo
    await _register(client, email="t@example.com", username="tank")
    token = (await _login(client, "tank")).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}
    # 撞到别人的用户名
    resp = await client.patch("/api/users/me/username", json={"username": "neo"}, headers=h)
    assert resp.status_code == 400 and resp.json()["detail"] == "USERNAME_TAKEN"
    # 格式非法
    resp = await client.patch("/api/users/me/username", json={"username": "AB"}, headers=h)
    assert resp.status_code == 422
