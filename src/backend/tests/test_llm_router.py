"""LLM 路由器测试：DB 路由优先、fake 回退、能力型环节不回退 default、用量记账。"""

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm.base import Message
from app.core.llm.router import LLMRouter
from app.core.security import encrypt_secret
from app.models.llm_config import LLMProviderConfig, LLMUsage, ModelRoute
from app.services.llm_admin import mask_api_key


async def test_fallback_to_fake_provider(app):
    router = LLMRouter()
    result = await router.complete("default", [Message(role="user", content="你好 Polaris")])
    assert result.model == "fake-default"
    assert "你好 Polaris" in result.content
    assert result.usage["prompt_tokens"] > 0
    assert result.usage["completion_tokens"] > 0

    # 记账落库（无归属字段也记录 stage/model/tokens）
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(LLMUsage))).scalars().all()
        assert len(rows) == 1
        assert rows[0].stage == "default"
        assert rows[0].model == "fake-default"


async def test_db_route_takes_precedence(app):
    async with get_sessionmaker()() as session:
        provider = LLMProviderConfig(
            name="fake-db",
            kind="fake",
            api_key_encrypted=encrypt_secret("sk-secret"),
            enabled=True,
        )
        session.add(provider)
        await session.flush()
        session.add(ModelRoute(stage="navigator", provider_id=provider.id, model="fake-db-model"))
        await session.commit()

    router = LLMRouter()
    result = await router.complete("navigator", [Message(role="user", content="plan it")])
    assert result.model == "fake-db-model"
    # 未配置的 stage 回退 default/fake
    result = await router.complete("writing", [Message(role="user", content="draft")])
    assert result.model == "fake-default"


async def test_unset_stage_falls_back_to_default_route(app):
    """未显式设置路由行的 stage 用 default 行（有 default 时不落到 fake）。"""
    async with get_sessionmaker()() as session:
        provider = LLMProviderConfig(name="fake-db", kind="fake", enabled=True)
        session.add(provider)
        await session.flush()
        session.add(ModelRoute(stage="default", provider_id=provider.id, model="fake-db-default"))
        await session.commit()

    router = LLMRouter()
    result = await router.complete("writing", [Message(role="user", content="draft")])
    assert result.model == "fake-db-default"


async def test_capability_stage_does_not_fall_back_to_default(app):
    """embedding/rerank 是能力型环节：配了 default 也不回退，未配置即抛 NotImplementedError。"""
    async with get_sessionmaker()() as session:
        provider = LLMProviderConfig(name="fake-db", kind="fake", enabled=True)
        session.add(provider)
        await session.flush()
        session.add(ModelRoute(stage="default", provider_id=provider.id, model="fake-db-default"))
        await session.commit()

    router = LLMRouter()
    with pytest.raises(NotImplementedError, match="embedding"):
        await router.embed(["some text"])
    with pytest.raises(NotImplementedError, match="rerank"):
        await router.rerank("q", ["doc"])
    # 普通 stage 回退 default 不受影响
    result = await router.complete("writing", [Message(role="user", content="draft")])
    assert result.model == "fake-db-default"


async def test_capability_stage_explicit_route_works(app):
    """显式配置 embedding/rerank 路由后正常解析。"""
    async with get_sessionmaker()() as session:
        provider = LLMProviderConfig(name="fake-db", kind="fake", enabled=True)
        session.add(provider)
        await session.flush()
        session.add(ModelRoute(stage="default", provider_id=provider.id, model="fake-db-default"))
        session.add(ModelRoute(stage="embedding", provider_id=provider.id, model="fake-embed"))
        session.add(ModelRoute(stage="rerank", provider_id=provider.id, model="fake-rerank"))
        await session.commit()

    router = LLMRouter()
    vectors = await router.embed(["some text"])
    assert len(vectors) == 1 and len(vectors[0]) > 0
    ranked = await router.rerank("q", ["doc a", "doc b"])
    assert len(ranked) == 2


async def test_capability_stage_fake_fallback_when_routes_empty(app):
    """路由表整体为空（未初始化环境/测试）→ 能力型环节仍回退确定性 fake。"""
    router = LLMRouter()
    vectors = await router.embed(["some text"])
    assert len(vectors) == 1 and len(vectors[0]) > 0


async def test_stream_records_usage(app):
    router = LLMRouter()
    chunks = [c async for c in router.stream("default", [Message(role="user", content="流式")])]
    assert "".join(chunks)
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(LLMUsage))).scalars().all()
        assert len(rows) == 1


def test_mask_api_key():
    assert mask_api_key("") == ""
    assert mask_api_key("short") == "***"
    assert mask_api_key("sk-abcdef1234567890abcd") == "sk-...abcd"
