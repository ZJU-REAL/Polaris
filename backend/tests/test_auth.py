from tests.conftest import INVITE_CODE, register_and_login


def _register_body(**overrides):
    body = {
        "email": "bob@example.com",
        "password": "str0ng-password",
        "display_name": "Bob",
        "invite_code": INVITE_CODE,
    }
    body.update(overrides)
    return body


async def test_register_without_invite_code_fails(client):
    body = _register_body()
    del body["invite_code"]
    resp = await client.post("/api/auth/register", json=body)
    assert resp.status_code == 422  # invite_code 必填


async def test_register_with_wrong_invite_code_fails(client):
    resp = await client.post("/api/auth/register", json=_register_body(invite_code="nope"))
    assert resp.status_code == 403
    assert resp.json()["detail"] == "INVALID_INVITE_CODE"


async def test_register_login_me_flow(client):
    resp = await client.post("/api/auth/register", json=_register_body())
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "bob@example.com"
    assert body["display_name"] == "Bob"
    assert body["role"] == "member"
    assert "invite_code" not in body

    # 重复注册
    resp = await client.post("/api/auth/register", json=_register_body())
    assert resp.status_code == 400

    # 登录拿 JWT（OAuth2 form）
    resp = await client.post(
        "/api/auth/jwt/login",
        data={"username": "bob@example.com", "password": "str0ng-password"},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]

    # /users/me
    resp = await client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "bob@example.com"


async def test_me_requires_auth(client):
    resp = await client.get("/api/users/me")
    assert resp.status_code == 401


async def test_login_wrong_password(client):
    await register_and_login(client, email="carol@example.com")
    resp = await client.post(
        "/api/auth/jwt/login",
        data={"username": "carol@example.com", "password": "wrong"},
    )
    assert resp.status_code == 400
