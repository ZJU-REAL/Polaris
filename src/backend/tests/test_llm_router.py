"""LLM 路由器测试：DB 路由优先、fake 回退、能力型环节不回退 default、用量记账。"""

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm.base import CompletionResult, Message
from app.core.llm.fake import FakeProvider
from app.core.llm.router import STAGES, STREAM_STAGES, LLMRouter
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


def test_stage_catalog():
    """环节清单：interview 已废弃移除；extract（结构化抽取）已就位。"""
    assert "interview" not in STAGES
    assert "extract" in STAGES
    assert "librarian" in STAGES
    assert "feedback_issue" in STAGES
    assert len(set(STAGES)) == len(STAGES)  # 无重复


def test_extract_stage_is_not_streamed():
    """extract 是短 JSON 抽取：不进流式广播（否则 JSON 会灌满任务终端）。"""
    assert "extract" not in STREAM_STAGES
    assert "librarian" in STREAM_STAGES  # 图文精读编译仍逐段广播


async def test_extract_stage_falls_back_to_default_route(app):
    """extract 不是能力型环节：未单独配置时平滑跟随 default。"""
    async with get_sessionmaker()() as session:
        provider = LLMProviderConfig(name="fake-db", kind="fake", enabled=True)
        session.add(provider)
        await session.flush()
        session.add(ModelRoute(stage="default", provider_id=provider.id, model="fake-db-default"))
        await session.commit()

    router = LLMRouter()
    result = await router.complete("extract", [Message(role="user", content="抽点 JSON")])
    assert result.model == "fake-db-default"
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(LLMUsage))).scalars().all()
        assert [r.stage for r in rows] == ["extract"]


def test_mask_api_key():
    assert mask_api_key("") == ""
    assert mask_api_key("short") == "***"
    assert mask_api_key("sk-abcdef1234567890abcd") == "sk-...abcd"


async def test_route_effort_reaches_provider(app):
    """路由表上配的 effort 透传给 provider；显式传参覆盖路由默认。"""
    async with get_sessionmaker()() as session:
        provider = LLMProviderConfig(name="fake-db", kind="fake", enabled=True)
        session.add(provider)
        await session.flush()
        session.add(
            ModelRoute(
                stage="default", provider_id=provider.id, model="fake-db-model", effort="high"
            )
        )
        await session.commit()

    router = LLMRouter()
    _, route = await router.resolve("default")
    assert route.effort == "high"

    seen: list[str | None] = []

    class _Recorder(FakeProvider):
        async def complete(self, messages, **kwargs):  # type: ignore[override]
            seen.append(kwargs.get("effort"))
            return await super().complete(messages, **kwargs)

    router._providers[("fake", None, "fake-db-model")] = _Recorder()
    router._provider_for = lambda r: router._providers[("fake", None, "fake-db-model")]  # type: ignore[assignment]

    await router.complete("default", [Message(role="user", content="hi")])
    await router.complete("default", [Message(role="user", content="hi")], effort="low")
    assert seen == ["high", "low"]


async def test_route_without_effort_sends_nothing(app):
    """未配 effort 的路由不给 provider 传该参数（老 provider 替身也不会因此炸掉）。"""
    async with get_sessionmaker()() as session:
        provider = LLMProviderConfig(name="fake-db", kind="fake", enabled=True)
        session.add(provider)
        await session.flush()
        session.add(ModelRoute(stage="default", provider_id=provider.id, model="fake-db-model"))
        await session.commit()

    seen_kwargs: list[dict] = []

    class _LegacyProvider(FakeProvider):
        """故意不声明 effort 形参：模拟未跟进新接口的 provider 子类/测试替身。"""

        async def complete(self, messages, *, model, temperature=0.7, max_tokens=None):  # type: ignore[override]
            seen_kwargs.append({"model": model, "temperature": temperature})
            return CompletionResult(content="ok", model=model)

    router = LLMRouter()
    legacy = _LegacyProvider()
    router._provider_for = lambda r: legacy  # type: ignore[assignment]
    result = await router.complete("default", [Message(role="user", content="hi")])
    assert result.content == "ok"
    assert len(seen_kwargs) == 1
