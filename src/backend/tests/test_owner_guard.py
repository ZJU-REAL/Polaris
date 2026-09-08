"""admin 面的单一主人守卫（#722）：桌面档=本人；服务器档=首位注册用户。

#616 移除 require_admin 后 server 档任何注册用户都能改全局 LLM 密钥/源密钥/向量空间
（审计 #715 第 9 项）；这里钉住恢复后的口径。
"""

from app.core.config import get_settings
from tests.conftest import register_and_login

# 两个 router 各取一个探针端点：守卫挂在 router 级，一个通则全通、一个堵则全堵。
ADMIN_PROBES = [
    ("GET", "/api/admin/settings/affiliation-mode"),
    ("GET", "/api/admin/llm/providers"),
]


async def _owner_and_second(client):
    owner = await register_and_login(client, email="owner@example.com")
    second = await register_and_login(client, email="second@example.com")
    return {"Authorization": f"Bearer {owner}"}, {"Authorization": f"Bearer {second}"}


async def test_server_profile_only_first_user_passes_admin(client):
    owner, second = await _owner_and_second(client)
    for method, url in ADMIN_PROBES:
        # 第二个用户先发请求也占不了坑：解析出的 owner 仍是 created_at 最早的首位用户
        resp = await client.request(method, url, headers=second)
        assert resp.status_code == 403, (method, url, resp.status_code)
        assert resp.json()["detail"] == "OWNER_REQUIRED"
        resp = await client.request(method, url, headers=owner)
        assert resp.status_code == 200, (method, url, resp.status_code)


async def test_server_profile_daily_admin_endpoints_are_owner_only(client):
    owner, second = await _owner_and_second(client)
    resp = await client.put("/api/daily/retention", json={"days": 21}, headers=second)
    assert resp.status_code == 403 and resp.json()["detail"] == "OWNER_REQUIRED"
    # 读端点保持全员开放
    resp = await client.get("/api/daily/retention", headers=second)
    assert resp.status_code == 200
    resp = await client.put("/api/daily/retention", json={"days": 21}, headers=owner)
    assert resp.status_code == 200 and resp.json() == {"days": 21}


async def test_desktop_profile_any_user_is_owner(client, monkeypatch):
    """桌面档唯一用户即机器的主人：守卫恒放行，不查库。"""
    owner, second = await _owner_and_second(client)
    monkeypatch.setattr(get_settings(), "profile", "desktop")
    for method, url in ADMIN_PROBES:
        resp = await client.request(method, url, headers=second)
        assert resp.status_code == 200, (method, url, resp.status_code)


async def test_admin_endpoints_still_require_login(client):
    for method, url in ADMIN_PROBES:
        resp = await client.request(method, url)
        assert resp.status_code == 401, (method, url, resp.status_code)
