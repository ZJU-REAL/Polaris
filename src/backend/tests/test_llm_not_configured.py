"""未配置 LLM 且未开 fake 回退：明确报错，不再产出演示假内容（issue #140）。

测试套件全局开着 POLARIS_LLM_FAKE_FALLBACK=1（conftest），
这里用 monkeypatch 临时关掉模拟生产默认行为。
"""

import pytest

from app.core.config import get_settings
from app.core.llm.router import LLMNotConfiguredError, get_llm_router, reset_llm_router
from tests.conftest import register_and_login


@pytest.fixture
def no_fake_fallback(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_fake_fallback", False)
    reset_llm_router()
    yield
    reset_llm_router()


async def test_resolve_raises_when_unconfigured(client, no_fake_fallback):
    router = get_llm_router()
    with pytest.raises(LLMNotConfiguredError):
        await router.resolve("default")
    # 能力型环节保持 NotImplementedError：调用方按既有降级路径处理（关键词检索等）
    with pytest.raises(NotImplementedError):
        await router.resolve("embedding")


async def test_resolve_falls_back_to_fake_when_enabled(client):
    # conftest 默认开启回退：无任何 DB 配置也能解析到确定性 fake
    reset_llm_router()
    _, route = await get_llm_router().resolve("default")
    assert route.provider_kind == "fake"


async def test_effective_test_reports_unconfigured(client, no_fake_fallback):
    token = await register_and_login(client)
    r = await client.post(
        "/api/me/llm/test-effective",
        json={"stage": "default"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == "NO_REAL_MODEL"
    assert body["is_fake"] is True
