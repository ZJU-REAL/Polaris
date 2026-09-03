"""desktop 档位免登录（POST /auth/local-session）。"""

import pytest

from app.core.config import Settings

pytestmark = pytest.mark.asyncio


def _desktop_settings():
    # 其余字段（secret_key/database_url 等）沿用 conftest 注入的测试环境变量，
    # 只切档位——token 校验与其他端点共用同一 secret。
    return Settings(profile="desktop")


async def test_local_session_absent_in_server_profile(client):
    resp = await client.post("/api/auth/local-session")
    assert resp.status_code == 404


async def test_local_session_provisions_admin_and_is_idempotent(client, monkeypatch):
    monkeypatch.setattr("app.api.auth.get_settings", _desktop_settings)

    resp = await client.post("/api/auth/local-session")
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    me = await client.get("/api/users/me", headers=headers)
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["email"] == "local@polaris.desktop"
    assert body["role"] == "admin"

    # 幂等：第二次不再建新用户，会话仍指向同一身份
    resp2 = await client.post("/api/auth/local-session")
    assert resp2.status_code == 200
    me2 = await client.get(
        "/api/users/me", headers={"Authorization": f"Bearer {resp2.json()['access_token']}"}
    )
    assert me2.json()["id"] == body["id"]

    # 会话能用于受保护的写端点（建课题）
    project = await client.post(
        "/api/projects", json={"name": "本地课题", "statement": "s"}, headers=headers
    )
    assert project.status_code == 201, project.text


async def test_capabilities_reports_local_session(client, monkeypatch):
    resp = await client.get("/api/auth/capabilities")
    assert resp.json()["local_session"] is False
    monkeypatch.setattr("app.api.auth.get_settings", _desktop_settings)
    resp = await client.get("/api/auth/capabilities")
    assert resp.json()["local_session"] is True
