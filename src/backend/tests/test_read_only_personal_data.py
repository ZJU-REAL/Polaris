"""只读账号（游客）看不到实验室成员的个人信息：名册与按邮箱查人都拒。"""

from tests.conftest import register_and_login


async def _make_guest(client, admin_token: str) -> str:
    resp = await client.post(
        "/api/admin/users",
        json={
            "email": "guest@example.com",
            "password": "gu3st-pass",
            "display_name": "Guest",
            "username": "guest",
            "role": "admin",
            "read_only": True,
        },
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert resp.status_code == 201, resp.text
    login = await client.post(
        "/api/auth/jwt/login", data={"username": "guest", "password": "gu3st-pass"}
    )
    assert login.status_code == 200, login.text
    return login.json()["access_token"]


async def test_guest_cannot_read_the_roster(client):
    admin = await register_and_login(client)
    guest = await _make_guest(client, admin)
    gh = {"Authorization": f"Bearer {guest}"}

    # 名册：管理员看得到，游客看不到
    listed = await client.get("/api/admin/users", headers=gh)
    assert listed.status_code == 403
    assert listed.json()["detail"] == "READ_ONLY_NO_PERSONAL_DATA"

    # 换个入口也拿不到同一份东西：按邮箱模糊查人会返回成员邮箱
    found = await client.get("/api/collaborators/search?q=example", headers=gh)
    assert found.status_code == 403
    assert found.json()["detail"] == "READ_ONLY_NO_PERSONAL_DATA"


async def test_guest_cannot_read_the_registration_codes(client):
    """注册码是明文，抄走就能开真账号——只读挡得住写，挡不住它被读走。"""
    admin = await register_and_login(client)
    guest = await _make_guest(client, admin)

    codes = await client.get(
        "/api/admin/registration-codes", headers={"Authorization": f"Bearer {guest}"}
    )
    assert codes.status_code == 403
    # 与名册用不同的错误码：理由不同，一个是「有谁」，一个是「拿了就能用」
    assert codes.json()["detail"] == "READ_ONLY_NO_SECRETS"


async def test_guest_still_sees_the_non_personal_admin_screens(client):
    """挡的是「实验室里有谁」，不是「管理端长什么样」——游客仍该看得见配置。"""
    admin = await register_and_login(client)
    guest = await _make_guest(client, admin)
    gh = {"Authorization": f"Bearer {guest}"}

    for path in (
        "/api/admin/llm/providers",  # 模型配置：api_key 是脱敏的，没有人的信息
        "/api/admin/settings/experiment-env",
    ):
        resp = await client.get(path, headers=gh)
        assert resp.status_code == 200, f"{path} -> {resp.status_code} {resp.text}"


async def test_admins_and_members_are_unaffected(client):
    """闸门只认 read_only：真管理员照常看名册，普通成员照常查协作者。"""
    admin = await register_and_login(client)
    ah = {"Authorization": f"Bearer {admin}"}
    assert (await client.get("/api/admin/users", headers=ah)).status_code == 200
    assert (await client.get("/api/collaborators/search?q=example", headers=ah)).status_code == 200
    assert (await client.get("/api/admin/registration-codes", headers=ah)).status_code == 200
