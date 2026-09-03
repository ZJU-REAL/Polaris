"""只读账号（游客）看不到实验室成员的个人信息：按邮箱查人拒之门外。"""

from tests.conftest import register_and_login, set_user_fields


async def _make_guest(client) -> str:
    # 管理端建号 API 已随去实验室化移除（#603）：先正常注册，再直接写库
    # 摆成游客（role=admin 才看得见管理端，read_only 才写不动）。
    token = await register_and_login(client, email="guest@example.com", username="guest")
    await set_user_fields("guest@example.com", role="admin", read_only=True)
    return token


async def test_guest_cannot_search_members(client):
    await register_and_login(client)
    guest = await _make_guest(client)
    gh = {"Authorization": f"Bearer {guest}"}

    # 按邮箱模糊查人会返回成员邮箱——对游客一律拒。
    # （成员名册 GET /api/admin/users 已随管理端一起移除，不必再守那个口。）
    found = await client.get("/api/collaborators/search?q=example", headers=gh)
    assert found.status_code == 403
    assert found.json()["detail"] == "READ_ONLY_NO_PERSONAL_DATA"


async def test_guest_still_sees_the_non_personal_admin_screens(client):
    """挡的是「实验室里有谁」，不是「管理端长什么样」——游客仍该看得见配置。"""
    await register_and_login(client)
    guest = await _make_guest(client)
    gh = {"Authorization": f"Bearer {guest}"}

    for path in (
        "/api/admin/llm/providers",  # 模型配置：api_key 是脱敏的，没有人的信息
        "/api/admin/settings/experiment-env",
    ):
        resp = await client.get(path, headers=gh)
        assert resp.status_code == 200, f"{path} -> {resp.status_code} {resp.text}"


async def test_admins_and_members_are_unaffected(client):
    """闸门只认 read_only：普通登录用户照常查协作者。"""
    admin = await register_and_login(client)
    ah = {"Authorization": f"Bearer {admin}"}
    assert (await client.get("/api/collaborators/search?q=example", headers=ah)).status_code == 200
