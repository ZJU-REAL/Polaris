"""管理端 LLM 配置测试：providers CRUD / routes / usage 聚合 / test-model / 权限。"""

import uuid

import pytest
from cryptography.fernet import InvalidToken

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.models.llm_config import LLMUsage
from app.services import llm_admin
from tests.conftest import register_and_login

API_KEY = "sk-abcdef1234567890abcd"


def test_masked_key_of_survives_encryption_key_rotation(monkeypatch):
    provider = llm_admin.LLMProviderConfig(
        name="rotated",
        kind="openai_compat",
        api_key_encrypted="encrypted-with-old-key",
    )

    def stale_secret(_token):
        raise InvalidToken

    monkeypatch.setattr(llm_admin, "decrypt_secret", stale_secret)

    assert llm_admin.masked_key_of(provider) == "*** (needs reconfiguration)"


def test_masked_key_of_survives_real_key_rotation(monkeypatch):
    """真跑一次轮换，不 mock 异常：确认坏掉的 token 走的确实是 InvalidToken 这条路。

    上一条用例把 decrypt_secret 换成直接 raise，证明的是"接住 InvalidToken 会怎样"，
    而不是"轮换后真的抛 InvalidToken"。两件事都得钉住，否则收窄 except 时没有依据。
    """
    from cryptography.fernet import Fernet

    from app.core import security

    old_key, new_key = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    settings = get_settings()

    monkeypatch.setattr(settings, "encryption_key", old_key, raising=False)
    security.get_fernet.cache_clear()
    stale = security.encrypt_secret(API_KEY)

    monkeypatch.setattr(settings, "encryption_key", new_key, raising=False)
    security.get_fernet.cache_clear()
    try:
        provider = llm_admin.LLMProviderConfig(
            name="rotated", kind="openai_compat", api_key_encrypted=stale
        )
        assert llm_admin.masked_key_of(provider) == "*** (needs reconfiguration)"
    finally:
        security.get_fernet.cache_clear()


def test_masked_key_of_surfaces_a_misconfigured_server_key(monkeypatch):
    """服务端 POLARIS_ENCRYPTION_KEY 本身配错时不能伪装成"这个 provider 要重填"。

    Fernet.decrypt 对任何坏 token 抛的都是 InvalidToken；ValueError 只会来自
    Fernet(key) 构造，也就是部署配错。把它一起接住的话，每个 provider 都会显示
    "needs reconfiguration"，管理员照着重填时 encrypt_secret 抛同一个 ValueError 报
    500，而真正的原因（密钥格式不对）已经被吞掉，排查会从错误的方向开始。
    """
    from app.core import security

    monkeypatch.setattr(get_settings(), "encryption_key", "not-a-real-key", raising=False)
    security.get_fernet.cache_clear()
    try:
        provider = llm_admin.LLMProviderConfig(
            name="broken", kind="openai_compat", api_key_encrypted="whatever"
        )
        with pytest.raises(ValueError, match="Fernet key"):
            llm_admin.masked_key_of(provider)
    finally:
        security.get_fernet.cache_clear()


async def _admin_and_member(client):
    admin_token = await register_and_login(client, email="admin@example.com")  # 首个 → admin
    member_token = await register_and_login(client, email="member@example.com")
    return (
        {"Authorization": f"Bearer {admin_token}"},
        {"Authorization": f"Bearer {member_token}"},
    )


# ---- test-model ----


async def _fake_provider_id(client, admin) -> str:
    resp = await client.post(
        "/api/admin/llm/providers", json={"name": "fake", "kind": "fake"}, headers=admin
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _openai_provider_id(client, admin) -> str:
    resp = await client.post(
        "/api/admin/llm/providers",
        json={
            "name": "relay",
            "kind": "openai_compat",
            "base_url": "http://relay.test/v1",
            "api_key": API_KEY,
        },
        headers=admin,
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def test_usage_aggregation(client):
    admin, _ = await _admin_and_member(client)
    user_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        for _i in range(3):
            session.add(
                LLMUsage(
                    user_id=None,
                    project_id=None,
                    voyage_id=None,
                    stage="navigator",
                    model="fake-default",
                    prompt_tokens=100,
                    completion_tokens=50,
                )
            )
        session.add(
            LLMUsage(
                user_id=None,
                project_id=None,
                voyage_id=None,
                stage="sextant",
                model="fake-default",
                prompt_tokens=10,
                completion_tokens=5,
            )
        )
        await session.commit()

    resp = await client.get("/api/admin/llm/usage?days=7", headers=admin)
    assert resp.status_code == 200
    rows = {r["stage"]: r for r in resp.json()}
    assert rows["navigator"]["prompt_tokens"] == 300
    assert rows["navigator"]["completion_tokens"] == 150
    assert rows["navigator"]["calls"] == 3
    assert rows["sextant"]["calls"] == 1

    # user 过滤（无匹配 → 空）
    resp = await client.get(f"/api/admin/llm/usage?user_id={user_id}", headers=admin)
    assert resp.json() == []
