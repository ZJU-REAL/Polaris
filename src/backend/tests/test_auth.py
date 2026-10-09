"""会话与本人资料（#842：单人本地产品，没有注册/密码登录/邮箱验证码）。"""

import pytest

from app.api.auth import LOCAL_USER_EMAIL
from tests.conftest import register_and_login


async def test_local_session_is_idempotent_and_returns_one_user(client):
    first = await client.post("/api/auth/local-session")
    second = await client.post("/api/auth/local-session")
    assert first.status_code == second.status_code == 200
    ids = set()
    for resp in (first, second):
        token = resp.json()["access_token"]
        me = await client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200
        body = me.json()
        assert body["email"] == LOCAL_USER_EMAIL
        assert body["username"] == "local"
        assert "is_superuser" not in body
        ids.add(body["id"])
    assert len(ids) == 1


async def test_me_requires_auth(client):
    resp = await client.get("/api/users/me")
    assert resp.status_code == 401


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/auth/register"),
        ("post", "/api/auth/jwt/login"),
        ("post", "/api/auth/jwt/logout"),
        ("post", "/api/auth/send-code"),
        ("post", "/api/auth/reset-password"),
        ("get", "/api/auth/username-available"),
        ("get", "/api/auth/capabilities"),
    ],
)
async def test_account_endpoints_are_gone(client, method, path):
    resp = await getattr(client, method)(path)
    assert resp.status_code in (404, 405), resp.text


async def test_other_users_are_not_addressable(client):
    """fastapi-users 的通用 /users/{id} 管理路由已拆掉：只有 /users/me。"""
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    me = (await client.get("/api/users/me", headers=headers)).json()
    resp = await client.get(f"/api/users/{me['id']}", headers=headers)
    assert resp.status_code in (404, 405)


async def test_patch_me_only_changes_display_name(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.patch(
        "/api/users/me",
        json={"display_name": "  王小明 ", "email": "x@example.com", "is_active": False},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_name"] == "王小明"
    assert body["email"] == LOCAL_USER_EMAIL
    assert body["is_active"] is True

    resp = await client.patch("/api/users/me", json={"display_name": "  "}, headers=headers)
    assert resp.status_code == 422


async def test_username_change_once_then_locked(client):
    token = await register_and_login(client)
    h = {"Authorization": f"Bearer {token}"}
    me = (await client.get("/api/users/me", headers=h)).json()
    assert me["username_locked"] is False

    # 格式非法
    resp = await client.patch("/api/users/me/username", json={"username": "AB"}, headers=h)
    assert resp.status_code == 422

    resp = await client.patch("/api/users/me/username", json={"username": "trinity"}, headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["username"] == "trinity"
    assert resp.json()["username_locked"] is True

    resp = await client.patch("/api/users/me/username", json={"username": "morpheus"}, headers=h)
    assert resp.status_code == 400
    assert resp.json()["detail"] == "USERNAME_LOCKED"


async def test_session_lifetime_is_configurable_and_long_by_default():
    """会话默认 30 天；过期了前端收到 401 会自动再取一次本地会话。"""
    from app.api.auth import get_jwt_strategy
    from app.core.config import Settings, get_settings

    assert Settings().session_lifetime_seconds == 60 * 60 * 24 * 30
    assert get_jwt_strategy().lifetime_seconds == get_settings().session_lifetime_seconds


async def test_stdio_mcp_defaults_to_the_local_user(client, monkeypatch):
    """stdio MCP 不用配置也能跑：默认以本地用户身份执行，环境变量只做覆盖。"""
    from app.mcp.__main__ import _resolve_user_id

    monkeypatch.delenv("POLARIS_MCP_USER_EMAIL", raising=False)
    with pytest.raises(SystemExit):
        await _resolve_user_id()  # 一个用户都没有

    token = await register_and_login(client)
    me = (await client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})).json()
    assert str(await _resolve_user_id()) == me["id"]

    monkeypatch.setenv("POLARIS_MCP_USER_EMAIL", LOCAL_USER_EMAIL)
    assert str(await _resolve_user_id()) == me["id"]
    monkeypatch.setenv("POLARIS_MCP_USER_EMAIL", "ghost@example.com")
    with pytest.raises(SystemExit):
        await _resolve_user_id()
