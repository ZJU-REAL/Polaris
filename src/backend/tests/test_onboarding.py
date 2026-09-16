"""开场清单：新用户登录后还差哪几步（#801 第 3 步）。

清单的价值全在「每一项都从真实状态算」。存一个进度值的话，删掉唯一的文献库之后
它还停在「已完成」——那比不给这张卡片更糟，它把一件没发生的事说成发生了。

另一条判据：每一项指向的地方，这个用户得真能打开。指着一个他一点就 403 的页面，
等于把「你还没做」说成「你去做」，而他根本做不了。#803 之后模型那项才成立。
"""

from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.models.user import User
from tests.conftest import register_and_login


async def _auth(client, email="new@example.com"):
    token = await register_and_login(client, email=email)
    return {"Authorization": f"Bearer {token}"}


async def _checklist(client, headers) -> dict:
    resp = await client.get("/api/onboarding", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _item(data: dict, item_id: str) -> dict:
    return next(i for i in data["items"] if i["id"] == item_id)


async def test_anonymous_gets_no_checklist(client):
    assert (await client.get("/api/onboarding")).status_code == 401


async def test_a_brand_new_user_has_everything_left_to_do(client):
    headers = await _auth(client)
    data = await _checklist(client, headers)
    assert [i["id"] for i in data["items"]] == ["model", "library", "discipline", "experiment"]
    assert all(not i["done"] for i in data["items"])
    assert data["done"] is False
    assert data["dismissed"] is False


async def test_creating_a_library_ticks_the_library_item(client):
    """从真实状态算：建一个库，这一项立刻变成已完成，不需要谁去「标记完成」。"""
    headers = await _auth(client)
    assert _item(await _checklist(client, headers), "library")["done"] is False

    resp = await client.post(
        "/api/libraries",
        json={"name": "Agents", "statement": "Long-running agents"},
        headers=headers,
    )
    assert resp.status_code in (200, 201), resp.text
    assert _item(await _checklist(client, headers), "library")["done"] is True


async def test_the_discipline_item_needs_a_discipline_not_just_a_library(client):
    """建了库但没选学科，学科那项仍未完成——两件事分开问，因为抽取口径是另一件事。"""
    headers = await _auth(client)
    await client.post(
        "/api/libraries",
        json={"name": "General", "statement": "No particular field"},
        headers=headers,
    )
    data = await _checklist(client, headers)
    assert _item(data, "library")["done"] is True
    assert _item(data, "discipline")["done"] is False

    await client.post(
        "/api/libraries",
        json={
            "name": "Bridges",
            "statement": "Long-span bridge design",
            "discipline": "structural",
        },
        headers=headers,
    )
    assert _item(await _checklist(client, headers), "discipline")["done"] is True


async def test_the_model_item_reads_the_route_table_not_the_fake_fallback(client):
    """没有任何路由 = 未配置。

    测试套件开着 llm_fake_fallback，resolve 在空表时也会返回一个 fake provider。
    拿 resolve 成功当「配好了」，这一项在全新部署上会直接显示已完成，而用户一点
    AI 功能就是 LLM_NOT_CONFIGURED。
    """
    headers = await _auth(client)
    assert _item(await _checklist(client, headers), "model")["done"] is False


async def test_configuring_a_model_ticks_the_model_item(client):
    """第二个注册的人配自己那份也算数（#803）——否则这一项对他永远亮不了。"""
    owner = await _auth(client, "owner@example.com")
    member = await _auth(client, "member@example.com")
    assert _item(await _checklist(client, member), "model")["done"] is False

    resp = await client.post(
        "/api/admin/llm/providers",
        json={"name": "mine", "kind": "fake"},
        headers=member,
    )
    assert resp.status_code == 201, resp.text
    provider_id = resp.json()["id"]
    resp = await client.put(
        "/api/admin/llm/routes",
        json=[{"stage": "default", "provider_id": provider_id, "model": "fake-default"}],
        headers=member,
    )
    assert resp.status_code == 200, resp.text

    assert _item(await _checklist(client, member), "model")["done"] is True
    # 主人那份没被他连带影响
    assert _item(await _checklist(client, owner), "model")["done"] is False


async def test_the_deployment_model_counts_for_everyone(client):
    """自部署的形状：主人配好部署级路由，其他人不必再配一份。"""
    owner = await _auth(client, "owner@example.com")
    member = await _auth(client, "member@example.com")
    resp = await client.post(
        "/api/admin/llm/providers", json={"name": "platform", "kind": "fake"}, headers=owner
    )
    provider_id = resp.json()["id"]
    await client.put(
        "/api/admin/llm/routes",
        json=[{"stage": "default", "provider_id": provider_id, "model": "fake-default"}],
        headers=owner,
    )
    assert _item(await _checklist(client, member), "model")["done"] is True


async def test_dismissing_hides_the_prompt_but_not_the_facts(client):
    """收起来的是提示，不是事实：items 照常返回，未完成的仍然是未完成。"""
    headers = await _auth(client)
    resp = await client.post("/api/onboarding/dismiss", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dismissed"] is True
    assert _item(data, "library")["done"] is False

    # 再查一次仍然记着
    assert (await _checklist(client, headers))["dismissed"] is True


async def test_dismissal_survives_because_the_json_column_is_reassigned(client):
    """就地改 settings 这个 dict 不会被识别为脏数据，提交下去等于没存。"""
    headers = await _auth(client, "persist@example.com")
    await client.post("/api/onboarding/dismiss", headers=headers)

    async with get_sessionmaker()() as session:
        user = (
            await session.execute(select(User).where(User.email == "persist@example.com"))
        ).scalar_one()
        assert user.settings.get("onboarding.dismissed") is True


async def test_one_users_progress_is_not_another_users(client):
    headers_a = await _auth(client, "a@example.com")
    headers_b = await _auth(client, "b@example.com")
    await client.post(
        "/api/libraries", json={"name": "A", "statement": "A's work"}, headers=headers_a
    )
    assert _item(await _checklist(client, headers_a), "library")["done"] is True
    assert _item(await _checklist(client, headers_b), "library")["done"] is False


async def test_adding_a_machine_ticks_the_experiment_item(client):
    """通用的 Python 实验要一台 SSH 机器（python_ml 的 credential_kinds=("ssh",)）；
    电路、流体那几个后端在本机容器里跑，不需要——所以这一项问的是有没有凭据。"""
    headers = await _auth(client)
    assert _item(await _checklist(client, headers), "experiment")["done"] is False

    key = "-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n-----END OPENSSH PRIVATE KEY-----"
    resp = await client.post(
        "/api/ssh-credentials",
        json={
            "name": "lab box",
            "host": "10.0.0.2",
            "username": "researcher",
            "private_key": key,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert _item(await _checklist(client, headers), "experiment")["done"] is True
