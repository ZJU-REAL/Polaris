"""个人用量历史（/users/me/usage*）。

自管 LLM 轨（/me/llm/*）已并入平台配置（#621），相关用例随端点一起删除。
"""


from tests.conftest import register_and_login


async def test_my_usage_history_scoped_to_self(client):
    token = await register_and_login(client)
    h = {"Authorization": f"Bearer {token}"}
    # 无记录时返回空列表（不 500）
    r = await client.get("/api/users/me/usage/history?days=30", headers=h)
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    # summary 仍可用
    s = await client.get("/api/users/me/usage", headers=h)
    assert s.status_code == 200
    assert "tokens_used" in s.json()


async def test_usage_history_requires_auth(client):
    r = await client.get("/api/users/me/usage/history")
    assert r.status_code == 401
