"""未配置 LLM 且未开 fake 回退：明确报错，不再产出演示假内容（issue #140/#717）。

测试套件全局开着 POLARIS_LLM_FAKE_FALLBACK=1（conftest），
这里用 monkeypatch 临时关掉模拟生产默认行为。
"""

import pytest

from app.core.config import Settings, get_settings
from app.core.llm.router import LLMNotConfiguredError, get_llm_router, reset_llm_router


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


def test_fake_fallback_is_strict_opt_in(monkeypatch):
    """#717：回退开关的唯一来源是显式设置——不设就关，设了就开，与 env/档位无关。

    曾经的 prod-only 强关守卫在 desktop 档（与 prod 互斥）永不触发，形同虚设，
    已删；信任模型改为「产品自身永不设置该变量」（设计报告 §18）。
    """
    monkeypatch.delenv("POLARIS_LLM_FAKE_FALLBACK", raising=False)
    # 不显式设：任何 env 都默认关闭
    assert Settings(env="prod", profile="server").llm_fake_fallback is False
    assert Settings(env="dev").llm_fake_fallback is False
    # 显式设 1：任何 env 都尊重显式 opt-in（不再按 env 强行改写）
    monkeypatch.setenv("POLARIS_LLM_FAKE_FALLBACK", "1")
    assert Settings(env="prod", profile="server").llm_fake_fallback is True
    assert Settings(env="dev").llm_fake_fallback is True
