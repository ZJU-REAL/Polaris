"""设置面的守卫（#842）：只要求登录（本地会话）；没登录一律 401。"""

from tests.conftest import register_and_login

# 每个 router 取一个探针端点：守卫挂在 router 级，一个通则全通、一个堵则全堵。
ADMIN_PROBES = [
    ("GET", "/api/admin/settings/affiliation-mode"),
    ("GET", "/api/admin/llm/usage"),
]


async def test_the_signed_in_user_can_use_the_settings(client):
    headers = {"Authorization": f"Bearer {await register_and_login(client)}"}
    for method, url in ADMIN_PROBES:
        resp = await client.request(method, url, headers=headers)
        assert resp.status_code == 200, (method, url, resp.status_code)
    resp = await client.put("/api/daily/retention", json={"days": 21}, headers=headers)
    assert resp.status_code == 200 and resp.json() == {"days": 21}


async def test_admin_endpoints_still_require_login(client):
    for method, url in ADMIN_PROBES:
        resp = await client.request(method, url)
        assert resp.status_code == 401, (method, url, resp.status_code)
