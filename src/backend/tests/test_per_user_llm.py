"""按用户的 LLM 配置：接管/自管切换、自己的 provider+路由、owner 感知的 resolve。"""

import uuid

from app.core.llm.router import get_llm_router
from tests.conftest import register_and_login


async def _me_id(client, token) -> uuid.UUID:
    resp = await client.get("/api/users/me", headers={"Authorization": f"Bearer {token}"})
    return uuid.UUID(resp.json()["id"])


async def _mk_provider(client, token, name, prefix="/api/admin/llm"):
    resp = await client.post(
        f"{prefix}/providers",
        json={"name": name, "kind": "fake"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _set_route(client, token, stage, provider_id, model, prefix="/api/admin/llm"):
    resp = await client.put(
        f"{prefix}/routes",
        json=[{"stage": stage, "provider_id": provider_id, "model": model}],
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text


async def test_status_default_managed_then_switch(client):
    token = await register_and_login(client)  # 首个用户=admin
    h = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/api/me/llm/status", headers=h)).json()["self_managed"] is False
    assert (await client.post("/api/me/llm/self-manage", headers=h)).json()["self_managed"] is True
    assert (await client.get("/api/users/me", headers=h)).json()["llm_self_managed"] is True
    assert (await client.post("/api/me/llm/managed", headers=h)).json()["self_managed"] is False


async def test_own_provider_crud_and_per_owner_name(client):
    admin = await register_and_login(client, email="admin@example.com")
    u1 = await register_and_login(client, email="u1@example.com")
    u2 = await register_and_login(client, email="u2@example.com")
    # 全局也叫 ds
    await _mk_provider(client, admin, "ds")
    # 两个用户各自的 ds 都允许
    p1 = await _mk_provider(client, u1, "ds", prefix="/api/me/llm")
    await _mk_provider(client, u2, "ds", prefix="/api/me/llm")
    # 同一用户重名 → 409
    dup = await client.post(
        "/api/me/llm/providers",
        json={"name": "ds", "kind": "fake"},
        headers={"Authorization": f"Bearer {u1}"},
    )
    assert dup.status_code == 409
    # 只看到自己的
    lst = await client.get("/api/me/llm/providers", headers={"Authorization": f"Bearer {u1}"})
    assert [p["id"] for p in lst.json()] == [p1]
    # 删除
    assert (
        await client.delete(
            f"/api/me/llm/providers/{p1}", headers={"Authorization": f"Bearer {u1}"}
        )
    ).status_code == 204


async def test_route_must_reference_own_provider(client):
    admin = await register_and_login(client, email="admin@example.com")
    member = await register_and_login(client, email="m@example.com")
    gid = await _mk_provider(client, admin, "global-p")  # 全局 provider
    # 用户拿全局 provider 建自己的路由 → 400（provider 不属于自己）
    resp = await client.put(
        "/api/me/llm/routes",
        json=[{"stage": "default", "provider_id": gid, "model": "x"}],
        headers={"Authorization": f"Bearer {member}"},
    )
    assert resp.status_code == 400
    # 用自己的 provider 就行
    mid = await _mk_provider(client, member, "mine", prefix="/api/me/llm")
    ok = await client.put(
        "/api/me/llm/routes",
        json=[{"stage": "default", "provider_id": mid, "model": "x"}],
        headers={"Authorization": f"Bearer {member}"},
    )
    assert ok.status_code == 200


async def test_effective_reflects_managed_vs_self(client):
    admin = await register_and_login(client, email="admin@example.com")
    member = await register_and_login(client, email="m@example.com")
    await _mk_provider(client, admin, "global-only")
    mh = {"Authorization": f"Bearer {member}"}
    # 被接管：effective = 全局
    eff = (await client.get("/api/me/llm/effective", headers=mh)).json()
    assert eff["self_managed"] is False
    assert [p["name"] for p in eff["providers"]] == ["global-only"]
    # 自管：effective = 自己的（此刻为空）
    await client.post("/api/me/llm/self-manage", headers=mh)
    eff2 = (await client.get("/api/me/llm/effective", headers=mh)).json()
    assert eff2["self_managed"] is True
    assert eff2["providers"] == []


async def test_admin_toggle_llm_self_managed(client):
    admin = await register_and_login(client, email="admin@example.com")
    member = await register_and_login(client, email="m@example.com")
    mid = await _me_id(client, member)
    ah = {"Authorization": f"Bearer {admin}"}
    r = await client.patch(f"/api/admin/users/{mid}", json={"llm_self_managed": True}, headers=ah)
    assert r.status_code == 200
    assert r.json()["llm_self_managed"] is True
    assert (
        await client.get("/api/me/llm/status", headers={"Authorization": f"Bearer {member}"})
    ).json()["self_managed"] is True


async def test_resolve_is_owner_aware(client):
    admin = await register_and_login(client, email="admin@example.com")
    member = await register_and_login(client, email="m@example.com")
    mid = await _me_id(client, member)
    # 全局 default → g-model
    gid = await _mk_provider(client, admin, "g")
    await _set_route(client, admin, "default", gid, "g-model")
    # 用户自管 default → u-model
    mh = {"Authorization": f"Bearer {member}"}
    await client.post("/api/me/llm/self-manage", headers=mh)
    uid_p = await _mk_provider(client, member, "u", prefix="/api/me/llm")
    await _set_route(client, member, "default", uid_p, "u-model", prefix="/api/me/llm")

    router = get_llm_router()
    router.invalidate_cache()
    # 自管用户 → 自己的
    _, route_member = await router.resolve("default", user_id=mid)
    assert route_member.model == "u-model"
    # 被接管用户（admin 自己，未自管）与无 user_id 的系统调用 → 全局
    _, route_admin = await router.resolve("default", user_id=await _me_id(client, admin))
    assert route_admin.model == "g-model"
    _, route_sys = await router.resolve("default", user_id=None)
    assert route_sys.model == "g-model"
    # 切回接管后，用户也走全局
    await client.post("/api/me/llm/managed", headers=mh)
    router.invalidate_cache()
    _, route_back = await router.resolve("default", user_id=mid)
    assert route_back.model == "g-model"
