"""公有云：第二个用户能配自己的模型（#801 第 1 步）。

今天的形状是「一台机器一个主人」：``require_owner`` 守着 LLM 配置，路由表只读
``owner_id IS NULL``。那对自部署是对的，但公有云里第二个注册的人连设置页都打不开
——「登录后提示你去配模型」于是只是一句空话，点进去是 403。

这些用例钉三件事：
1. 单主人部署的行为一字不变（回退底仍是部署级那张表）；
2. 第二个用户配得了自己的，而且只看得见自己的；
3. 拿不到别人的 key——既不能读，也不能把别人的 provider 写进自己的路由。
"""

import uuid

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.llm_config import LLMProviderConfig
from app.models.user import User
from tests.conftest import register_and_login


async def _auth(client, email):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def _user_id(email: str) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(select(User.id).where(User.email == email))
        ).scalar_one()


async def _make_provider(client, headers, name):
    resp = await client.post(
        "/api/admin/llm/providers",
        json={
            "name": name,
            "kind": "openai_compat",
            "base_url": "https://x/v1",
            "api_key": "sk-" + name,
        },
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


async def _set_routes(client, headers, items):
    resp = await client.put("/api/admin/llm/routes", json=items, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# --------------------------------------------------------------------------
# 归属
# --------------------------------------------------------------------------


async def test_the_owner_still_configures_the_deployment_table(client):
    """主人配的仍是 owner IS NULL 那张——它是所有人的回退底，不能悄悄变成私有行。"""
    owner = await _auth(client, "owner@example.com")
    provider_id = await _make_provider(client, owner, "platform")

    async with get_sessionmaker()() as session:
        row = await session.get(LLMProviderConfig, uuid.UUID(provider_id))
        assert row is not None and row.owner_id is None


async def test_a_second_user_can_configure_their_own(client):
    """此前这里是 403 OWNER_REQUIRED——公有云的第一道墙就在这儿。"""
    await _auth(client, "owner@example.com")
    member = await _auth(client, "member@example.com")

    provider_id = await _make_provider(client, member, "mine")

    async with get_sessionmaker()() as session:
        row = await session.get(LLMProviderConfig, uuid.UUID(provider_id))
        assert row is not None
        assert row.owner_id == await _user_id("member@example.com")


async def test_users_do_not_see_each_others_providers(client):
    owner = await _auth(client, "owner@example.com")
    member = await _auth(client, "member@example.com")
    await _make_provider(client, owner, "platform")
    await _make_provider(client, member, "mine")

    owner_rows = (await client.get("/api/admin/llm/providers", headers=owner)).json()
    member_rows = (await client.get("/api/admin/llm/providers", headers=member)).json()
    assert {p["name"] for p in owner_rows} == {"platform"}
    assert {p["name"] for p in member_rows} == {"mine"}


async def test_a_user_cannot_route_through_someone_elses_provider(client):
    """否则把别人的 provider id 写进自己的路由表，就是拿别人的 key 跑自己的任务。"""
    owner = await _auth(client, "owner@example.com")
    member = await _auth(client, "member@example.com")
    platform_provider = await _make_provider(client, owner, "platform")

    resp = await client.put(
        "/api/admin/llm/routes",
        json=[{"stage": "default", "provider_id": platform_provider, "model": "gpt-4o"}],
        headers=member,
    )
    assert resp.status_code == 400
    assert "provider not found" in resp.json()["detail"]


async def test_a_user_cannot_edit_someone_elses_provider(client):
    """按 404 而不是 403：说「存在但不归你」本身就在泄露别人配了什么。"""
    owner = await _auth(client, "owner@example.com")
    member = await _auth(client, "member@example.com")
    platform_provider = await _make_provider(client, owner, "platform")

    resp = await client.patch(
        f"/api/admin/llm/providers/{platform_provider}",
        json={"api_key": "sk-stolen"},
        headers=member,
    )
    assert resp.status_code == 404


async def test_usage_and_call_logs_stay_owner_only(client):
    """路由级守卫撤掉后逐个补回主人限制；漏一个就是把别人的调用记录交出去。"""
    await _auth(client, "owner@example.com")
    member = await _auth(client, "member@example.com")

    for path in (
        "/api/admin/llm/usage",
        "/api/admin/llm/call-logs",
        "/api/admin/llm/call-logs/settings",
    ):
        resp = await client.get(path, headers=member)
        assert resp.status_code == 403, f"{path} 应只对主人开放，实际 {resp.status_code}"


# --------------------------------------------------------------------------
# 选路：自己配的覆盖部署级的，逐条合并
# --------------------------------------------------------------------------


async def test_a_users_own_route_wins(client):
    owner = await _auth(client, "owner@example.com")
    member = await _auth(client, "member@example.com")
    platform_provider = await _make_provider(client, owner, "platform")
    own_provider = await _make_provider(client, member, "mine")

    await _set_routes(
        client,
        owner,
        [{"stage": "default", "provider_id": platform_provider, "model": "platform-model"}],
    )
    await _set_routes(
        client, member, [{"stage": "default", "provider_id": own_provider, "model": "my-model"}]
    )

    from app.core.llm.router import get_llm_router

    router = get_llm_router()
    router.invalidate_cache()
    _provider, route = await router.resolve("default", await _user_id("member@example.com"))
    assert route.model == "my-model"


async def test_an_unconfigured_stage_falls_back_to_the_deployment_route(client):
    """只配了一个环节的人，其余环节仍走部署级那份。

    整表二选一的话，「配了一个模型」会把他没碰过的环节一起变成未配置——
    界面上的表现是配完之后更多功能不能用了。
    """
    owner = await _auth(client, "owner@example.com")
    member = await _auth(client, "member@example.com")
    platform_provider = await _make_provider(client, owner, "platform")
    own_provider = await _make_provider(client, member, "mine")

    await _set_routes(
        client,
        owner,
        [
            {"stage": "default", "provider_id": platform_provider, "model": "platform-default"},
            {"stage": "embedding", "provider_id": platform_provider, "model": "platform-embed"},
        ],
    )
    await _set_routes(
        client, member, [{"stage": "default", "provider_id": own_provider, "model": "my-default"}]
    )

    from app.core.llm.router import get_llm_router

    router = get_llm_router()
    router.invalidate_cache()
    member_id = await _user_id("member@example.com")
    _p, own = await router.resolve("default", member_id)
    _p2, inherited = await router.resolve("embedding", member_id)
    assert own.model == "my-default"
    assert inherited.model == "platform-embed"


async def test_a_user_with_no_configuration_sees_the_deployment_table(client):
    """存量部署的每个用户都是这种情况——行为必须与改动之前一字不变。"""
    owner = await _auth(client, "owner@example.com")
    await _auth(client, "member@example.com")  # 注册即可：他什么都不配
    platform_provider = await _make_provider(client, owner, "platform")
    await _set_routes(
        client,
        owner,
        [{"stage": "default", "provider_id": platform_provider, "model": "platform-default"}],
    )

    from app.core.llm.router import get_llm_router

    router = get_llm_router()
    router.invalidate_cache()
    _p, route = await router.resolve("default", await _user_id("member@example.com"))
    assert route.model == "platform-default"
