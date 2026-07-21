"""我的模型：测试当前生效模型状态；个人用量历史。"""


from tests.conftest import register_and_login


async def _mk_provider(client, token, name, prefix="/api/admin/llm"):
    r = await client.post(
        f"{prefix}/providers",
        json={"name": name, "kind": "fake"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_effective_test_managed_uses_global(client):
    admin = await register_and_login(client)  # 首个=admin
    member = await register_and_login(client, email="m@example.com")
    gid = await _mk_provider(client, admin, "g")
    await client.put(
        "/api/admin/llm/routes",
        json=[{"stage": "default", "provider_id": gid, "model": "g-model"}],
        headers={"Authorization": f"Bearer {admin}"},
    )
    # 被接管成员测生效 default → 命中全局 g-model（fake provider 可探通 → ok）
    r = await client.post(
        "/api/me/llm/test-effective",
        json={"stage": "default"},
        headers={"Authorization": f"Bearer {member}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "g-model"
    assert body["provider_name"] == "g"
    assert body["ok"] is True
    assert body["is_fake"] is False


async def test_effective_test_unconfigured_self_managed_is_fake(client):
    admin = await register_and_login(client)
    member = await register_and_login(client, email="m@example.com")
    mh = {"Authorization": f"Bearer {member}"}
    await client.post("/api/me/llm/self-manage", headers=mh)  # 自管但没配 → 回退 fake
    r = await client.post("/api/me/llm/test-effective", json={"stage": "default"}, headers=mh)
    assert r.status_code == 200, r.text
    assert r.json()["is_fake"] is True
    assert r.json()["ok"] is False
    assert r.json()["error"] == "NO_REAL_MODEL"


async def test_effective_test_self_managed_uses_own(client):
    admin = await register_and_login(client)
    member = await register_and_login(client, email="m@example.com")
    mh = {"Authorization": f"Bearer {member}"}
    await client.post("/api/me/llm/self-manage", headers=mh)
    pid = await _mk_provider(client, member, "mine", prefix="/api/me/llm")
    await client.put(
        "/api/me/llm/routes",
        json=[{"stage": "default", "provider_id": pid, "model": "u-model"}],
        headers=mh,
    )
    r = await client.post("/api/me/llm/test-effective", json={"stage": "default"}, headers=mh)
    assert r.status_code == 200
    assert r.json()["model"] == "u-model"
    assert r.json()["provider_name"] == "mine"
    assert r.json()["ok"] is True


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
