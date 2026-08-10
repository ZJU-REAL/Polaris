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
    assert "forge_generate" in STAGES
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

    recorder = _Recorder()
    # _provider_for 现在按 (route, stage) 取——stage 决定超时/重试档位（call_profile）
    router._provider_for = lambda r, stage="": recorder  # type: ignore[assignment]

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
    router._provider_for = lambda r, stage="": legacy  # type: ignore[assignment]
    result = await router.complete("default", [Message(role="user", content="hi")])
    assert result.content == "ok"
    assert len(seen_kwargs) == 1


# ---- 按环节区分耐心程度（生产上 relevance p95 达 1215s 的直接原因）----


def test_short_json_stages_get_a_tight_call_budget():
    """打分/判定/抽取这类短 JSON 环节，最坏耗时要压在两分钟级以内。

    生产实测 relevance 中位 13.3s、p95 却有 1215s——4 次尝试 × 300s 超时 + 退避
    3+6+12 正好 1221s，也就是重试循环熬过四次五分钟超时。而打分协程全程占着一个
    数据库连接，连接池被卡死的协程占满，其余论文批量超时、状态停在 candidate。
    """
    from app.core.llm.router import call_profile

    for stage in ("relevance", "sextant", "extract", "concepts"):
        timeout, attempts = call_profile(stage)
        worst = timeout * attempts + 3 * (attempts - 1)  # 含指数退避
        assert worst <= 180, f"{stage} 最坏 {worst}s，太能等了"


def test_long_form_stages_stay_patient():
    """编译 / 写作 / 评审本来就要跑几分钟，别把它们一起收紧了。"""
    from app.core.llm.router import STREAM_STAGES, call_profile

    for stage in STREAM_STAGES:
        timeout, _ = call_profile(stage)
        assert timeout >= 300, f"{stage} 超时 {timeout}s 太短"


def test_profile_is_part_of_the_provider_cache_key():
    """长短两档必须各持一个客户端——否则先建的那个把超时定死给所有环节。"""
    from app.core.llm.router import LLMRouter, ResolvedRoute

    router = LLMRouter()
    route = ResolvedRoute(
        provider_kind="fake",
        base_url=None,
        api_key="",
        model="m",
        temperature=None,
        provider_name="fake",
        effort=None,
    )
    router._provider_for(route, "relevance")
    router._provider_for(route, "librarian")
    assert len(router._providers) == 2, "长短档共用了同一个客户端"


# ---- digest 拆成独立 stage（可单独设模型）----


def test_digest_gets_the_long_call_budget_without_streaming():
    """digest 输出 JSON，不该流式；但它一次为一批论文生成洞察，必须给长档预算。

    「要给长耐心」与「要流式播出去」是两件事。#229 里我把耐心绑在 STREAM_STAGES 上，
    digest 正是反例——流式会把整段 JSON 灌进任务终端日志，而 60 秒预算又盖不住它
    实测 313 秒的 p95。
    """
    from app.core.llm.router import STREAM_STAGES, call_profile

    timeout, attempts = call_profile("digest")
    assert timeout >= 300, f"digest 超时 {timeout}s 太短，实测 p95 就有 313s"
    assert attempts >= 3
    assert "digest" not in STREAM_STAGES, "digest 输出 JSON，不该流式"


async def test_digest_falls_back_to_default_not_to_librarian(client, monkeypatch):
    """没显式配 digest 路由时跟随 default，哪怕 librarian 配着别的模型。

    这里曾经回退 librarian（digest 是从它拆出来的），于是设置页说的和实际打的不是
    一回事：界面显示「每日研究简报 · 跟随默认」并列着 default 的模型名，调用却走
    librarian。2026-08-10 生产因此排查了半天——默认明明配的 qwen-flash，日志里
    却全是 gpt-5.6-luna。界面承诺什么，解析就得给什么。
    """
    from app.core.llm.router import LLMRouter, ResolvedRoute

    router = LLMRouter()
    librarian_route = ResolvedRoute(
        provider_kind="fake",
        base_url=None,
        api_key="",
        model="librarian-model",
        temperature=None,
        provider_name="fake",
        effort=None,
    )
    default_route = ResolvedRoute(
        provider_kind="fake",
        base_url=None,
        api_key="",
        model="default-model",
        temperature=None,
        provider_name="fake",
        effort=None,
    )

    async def fake_routes(owner_id):
        return {"librarian": librarian_route, "default": default_route}

    monkeypatch.setattr(router, "_get_routes", fake_routes)
    _provider, route = await router.resolve("digest")
    assert route.model == "default-model", "应当跟随 default，而不是偷偷继承 librarian"


async def test_an_explicit_digest_route_wins(client, monkeypatch):
    """显式配了 digest 就用它——拆成独立环节的意义就在这里。"""
    from app.core.llm.router import LLMRouter, ResolvedRoute

    router = LLMRouter()

    def _route(model):
        return ResolvedRoute(
            provider_kind="fake",
            base_url=None,
            api_key="",
            model=model,
            temperature=None,
            provider_name="fake",
            effort=None,
        )

    async def fake_routes(owner_id):
        return {"digest": _route("digest-model"), "librarian": _route("librarian-model")}

    monkeypatch.setattr(router, "_get_routes", fake_routes)
    _provider, route = await router.resolve("digest")
    assert route.model == "digest-model"


# ---- forge 候选生成：长 JSON，路由遵循统一的 default 回退规则 ----


def test_forge_generate_gets_the_long_call_budget_without_streaming():
    """一次生成多个完整 Idea 可能超过 60 秒，但 JSON 不应流式灌入终端。"""
    from app.core.llm.router import STREAM_STAGES, call_profile

    timeout, attempts = call_profile("forge_generate")
    assert timeout >= 300
    assert attempts >= 3
    assert "forge_generate" not in STREAM_STAGES
    assert call_profile("forge") == (60.0, 2), "分析和逐条评分仍应保持短调用预算"


async def test_forge_generate_falls_back_to_default_when_unset(client, monkeypatch):
    """未显式配置候选生成路由时跟随 default，与设置页展示保持一致。"""
    from app.core.llm.router import LLMRouter, ResolvedRoute

    router = LLMRouter()

    def _route(model):
        return ResolvedRoute(
            provider_kind="fake",
            base_url=None,
            api_key="",
            model=model,
            temperature=None,
            provider_name="fake",
            effort=None,
        )

    async def fake_routes(owner_id):
        return {
            "forge": _route("forge-model"),
            "default": _route("default-model"),
        }

    monkeypatch.setattr(router, "_get_routes", fake_routes)
    _provider, route = await router.resolve("forge_generate")
    assert route.model == "default-model", "应跟随 default，而不是隐藏继承 forge"
