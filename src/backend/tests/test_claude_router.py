"""路由器只认一个模型：每个环节都解析到 Claude（core/llm/router.py），以及只读的
模型状态接口（api/llm_status.py）。"""

import anthropic
import httpx
import httpx2
import pytest
import respx
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm.anthropic import (
    NONSTREAM_MAX_TOKENS,
    STREAM_MAX_TOKENS,
    AnthropicProvider,
)
from app.core.llm.base import CompletionResult, Message
from app.core.llm.budgets import FORGE_CONTEXT, LIBRARIAN_FULLTEXT
from app.core.llm.fake import FakeProvider
from app.core.llm.router import (
    STAGES,
    LLMNotConfiguredError,
    LLMRouter,
    context_window_for,
    register_plugin_stage,
    stage_effort,
    stage_max_tokens,
    unregister_plugin_stage,
)
from app.models.llm_config import LLMProviderConfig, LLMUsage, ModelRoute
from tests.conftest import register_and_login

GATEWAY = {"LITELLM_API_KEY": "sk-gw", "LITELLM_API_BASE": "http://lab.test"}
CHAT_STAGES = [s for s in STAGES if s not in ("embedding", "rerank")]


@pytest.mark.parametrize("stage", CHAT_STAGES)
async def test_every_chat_stage_resolves_to_claude(app, claude_settings, stage):
    claude_settings(**GATEWAY)
    provider, route = await LLMRouter().resolve(stage)
    assert isinstance(provider, AnthropicProvider)
    assert route.provider_kind == "anthropic"
    assert route.model == "claude-opus-5"
    assert route.provider_name == "claude"
    assert route.base_url == "http://lab.test" and route.api_key == "sk-gw"
    assert route.temperature is None
    assert route.context_window == 1_000_000
    assert route.effort == stage_effort(stage)


async def test_the_model_comes_from_settings_not_from_the_stage(app, claude_settings):
    claude_settings(**GATEWAY, POLARIS_CLAUDE_MODEL="claude-fable-5-1")
    router = LLMRouter()
    assert {(await router.resolve(s))[1].model for s in CHAT_STAGES} == {"claude-fable-5-1"}


async def test_database_provider_rows_are_no_longer_consulted(app, claude_settings):
    """旧版本留在库里的服务商和路由行不能再影响任何调用。"""
    async with get_sessionmaker()() as session:
        provider = LLMProviderConfig(
            name="old", kind="openai_compat", base_url="http://old.test/v1", enabled=True
        )
        session.add(provider)
        await session.flush()
        session.add(ModelRoute(stage="default", provider_id=provider.id, model="gpt-old"))
        session.add(ModelRoute(stage="writing", provider_id=provider.id, model="gpt-old"))
        await session.commit()
    claude_settings(**GATEWAY)
    router = LLMRouter()
    for stage in ("default", "writing"):
        _, route = await router.resolve(stage)
        assert (route.provider_kind, route.model) == ("anthropic", "claude-opus-5")


async def test_plugin_stages_resolve_to_claude_with_their_declared_tier(app, claude_settings):
    claude_settings(**GATEWAY)
    name = register_plugin_stage("chem", "extract", tier="long", fallback="extract")
    try:
        provider, route = await LLMRouter().resolve(name)
        assert isinstance(provider, AnthropicProvider) and route.model == "claude-opus-5"
        assert route.effort == "high"
        assert stage_max_tokens(name, 500) == STREAM_MAX_TOKENS
    finally:
        unregister_plugin_stage(name)


async def test_no_key_and_no_fake_fallback_is_llm_not_configured(app, claude_settings):
    claude_settings(POLARIS_LLM_FAKE_FALLBACK="0")
    router = LLMRouter()
    assert router.is_configured() is False
    with pytest.raises(LLMNotConfiguredError):
        await router.resolve("default")
    # 向量 / 重排不是「没配模型」，是「这项能力不可用」：调用方据此退回关键词检索
    with pytest.raises(NotImplementedError):
        await router.resolve("embedding")
    assert await router.model_name("default") is None
    assert await router.model_name("embedding") is None


async def test_fake_fallback_still_serves_every_stage_without_a_key(app):
    router = LLMRouter()
    for stage in STAGES:
        provider, route = await router.resolve(stage)
        assert isinstance(provider, FakeProvider) and route.provider_kind == "fake"
    # 回退不等于「配好了」
    assert router.is_configured() is False and await router.has_chat_model() is False


async def test_a_real_key_wins_over_the_fake_fallback(app, claude_settings):
    settings = claude_settings(**GATEWAY)
    assert settings.llm_fake_fallback is True
    provider, _ = await LLMRouter().resolve("default")
    assert isinstance(provider, AnthropicProvider)


async def test_override_provider_still_replaces_everything(app, claude_settings):
    claude_settings(**GATEWAY)
    router = LLMRouter()
    fake = FakeProvider()
    router.override_provider(fake)
    provider, route = await router.resolve("writing")
    assert provider is fake and route.model == "claude-opus-5"


# ---- 思考深度与 max_tokens ----


def test_effort_follows_the_stage_tier():
    assert stage_effort("relevance") == "low"
    assert stage_effort("extract") == "low"
    assert stage_effort("agent") == "medium"
    assert stage_effort("extract_skeleton") == "medium"
    assert stage_effort("writing") == "high"
    assert stage_effort("digest") == "high"


def test_max_tokens_never_drops_below_the_floor():
    # 短 JSON 环节传的小数字是给不思考的模型定的；Claude 的 max_tokens 含思考
    assert stage_max_tokens("relevance", 300) == NONSTREAM_MAX_TOKENS
    assert stage_max_tokens("relevance", None) == NONSTREAM_MAX_TOKENS
    # 长文本环节和助手走流式，给足余量
    assert stage_max_tokens("writing", 4096) == STREAM_MAX_TOKENS
    assert stage_max_tokens("agent", None) == STREAM_MAX_TOKENS
    # 调用方要得更多时听调用方的
    assert stage_max_tokens("extract", 30_000) == 30_000


async def test_complete_hands_the_stage_profile_to_the_provider(app, claude_settings):
    claude_settings(**GATEWAY)
    seen: list[dict] = []

    class Recorder(FakeProvider):
        async def complete(self, messages, *, model, **kwargs):  # type: ignore[override]
            seen.append({"model": model, **kwargs})
            return CompletionResult(content="ok", model=model)

    router = LLMRouter()
    router.override_provider(Recorder())
    await router.complete("relevance", [Message("user", "score")], max_tokens=200)
    await router.complete("writing", [Message("user", "draft")])
    await router.complete("writing", [Message("user", "draft")], effort="max")

    assert seen[0]["model"] == "claude-opus-5"
    assert seen[0]["effort"] == "low" and seen[0]["max_tokens"] == NONSTREAM_MAX_TOKENS
    assert seen[0]["temperature"] is None
    assert seen[1]["effort"] == "high" and seen[1]["max_tokens"] == STREAM_MAX_TOKENS
    assert seen[2]["effort"] == "max"

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(LLMUsage))).scalars().all()
    assert {r.model for r in rows} == {"claude-opus-5"} and len(rows) == 3


# ---- 上下文窗口与输入预算 ----


def test_context_window_table_is_conservative_for_unknown_models():
    assert context_window_for("claude-opus-5") == 1_000_000
    assert context_window_for("anthropic/claude-opus-5") == 1_000_000
    assert context_window_for("claude-haiku-4-5") == 200_000
    assert context_window_for("something-new") == 200_000


async def test_input_budgets_come_from_the_models_window(app, claude_settings):
    claude_settings(**GATEWAY)
    router = LLMRouter()
    assert await router.input_budget(LIBRARIAN_FULLTEXT) == 160_000
    assert await router.input_budget(FORGE_CONTEXT) == 48_000

    claude_settings(POLARIS_CLAUDE_MODEL="claude-haiku-4-5")
    small = LLMRouter()
    assert await small.input_budget(LIBRARIAN_FULLTEXT) == LIBRARIAN_FULLTEXT.default


async def test_input_budgets_fall_back_to_defaults_without_a_key(app):
    assert await LLMRouter().input_budget(LIBRARIAN_FULLTEXT) == LIBRARIAN_FULLTEXT.default


# ---- 向量与重排：网关的 OpenAI 兼容端点 ----


@respx.mock
async def test_embedding_and_rerank_use_the_gateway_models(app, claude_settings):
    claude_settings(**GATEWAY)
    embed = respx.post("http://lab.test/v1/embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
    )
    rerank = respx.post("http://lab.test/v1/rerank").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [{"index": 0, "relevance_score": 0.9}],
                "meta": {"billed_units": {"total_tokens": 42}},
            },
        )
    )
    router = LLMRouter()
    assert await router.embed(["hello"]) == [[0.1, 0.2]]
    assert await router.rerank("q", ["only doc"]) == [(0, 0.9)]

    import json

    assert json.loads(embed.calls[0].request.content)["model"] == "BGE-M3"
    assert json.loads(rerank.calls[0].request.content)["model"] == "BGE-Reranker-V2-M3"
    assert embed.calls[0].request.headers["authorization"] == "Bearer sk-gw"
    assert await router.model_name("embedding") == "BGE-M3"
    assert await router.model_name("rerank") == "BGE-Reranker-V2-M3"

    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(LLMUsage))).scalars().all()
    by_stage = {r.stage: r for r in rows}
    assert by_stage["embedding"].model == "BGE-M3"
    assert by_stage["rerank"].model == "BGE-Reranker-V2-M3"
    assert by_stage["rerank"].prompt_tokens == 42


async def test_direct_anthropic_has_no_embedding_or_rerank(app, claude_settings):
    """直连官方（没有网关地址）：对话照常，向量 / 重排明确不可用而不是崩。"""
    claude_settings(ANTHROPIC_API_KEY="sk-ant", POLARIS_LLM_FAKE_FALLBACK="0")
    router = LLMRouter()
    provider, route = await router.resolve("default")
    assert isinstance(provider, AnthropicProvider) and route.base_url is None
    for stage in ("embedding", "rerank"):
        with pytest.raises(NotImplementedError):
            await router.resolve(stage)
    with pytest.raises(NotImplementedError):
        await router.embed(["x"])


async def test_semantic_search_degrades_to_keyword_without_a_gateway(client, claude_settings):
    claude_settings(ANTHROPIC_API_KEY="sk-ant", POLARIS_LLM_FAKE_FALLBACK="0")
    from app.services import embedding as embedding_service

    assert await embedding_service.routed_model() is None


# ---- 状态接口 ----


async def _auth(client):
    token = await register_and_login(client, email="status@example.com")
    return {"Authorization": f"Bearer {token}"}


async def test_status_requires_login(client):
    assert (await client.get("/api/llm/status")).status_code == 401
    assert (await client.post("/api/llm/status/check")).status_code == 401


async def test_status_without_a_key(client):
    headers = await _auth(client)
    data = (await client.get("/api/llm/status", headers=headers)).json()
    assert data == {
        "configured": False,
        "model": "claude-opus-5",
        "base_host": "api.anthropic.com",
        "embedding": {"model": "BGE-M3", "available": False},
        "rerank": {"model": "BGE-Reranker-V2-M3", "available": False},
    }
    check = (await client.post("/api/llm/status/check", headers=headers)).json()
    assert check == {"ok": False, "latency_ms": None, "error": "not_configured", "detail": None}


async def test_status_with_a_gateway_key_shows_host_but_never_the_key(client, claude_settings):
    headers = await _auth(client)
    claude_settings(LITELLM_API_KEY="sk-very-secret", LITELLM_API_BASE="http://10.0.0.1:8010/v1")
    resp = await client.get("/api/llm/status", headers=headers)
    assert resp.json() == {
        "configured": True,
        "model": "claude-opus-5",
        "base_host": "10.0.0.1:8010",
        "embedding": {"model": "BGE-M3", "available": True},
        "rerank": {"model": "BGE-Reranker-V2-M3", "available": True},
    }
    assert "sk-very-secret" not in resp.text


async def test_status_with_a_direct_key_has_no_search_models(client, claude_settings):
    headers = await _auth(client)
    claude_settings(ANTHROPIC_API_KEY="sk-ant")
    data = (await client.get("/api/llm/status", headers=headers)).json()
    assert data["configured"] is True and data["base_host"] == "api.anthropic.com"
    assert data["embedding"]["available"] is False and data["rerank"]["available"] is False


def _status_error(cls, status: int, message: str = "boom"):
    request = httpx2.Request("POST", "http://lab.test/v1/messages")
    return cls(message, response=httpx2.Response(status, request=request), body=None)


async def test_check_reports_ok_with_latency(client, claude_settings, monkeypatch):
    headers = await _auth(client)
    claude_settings(**GATEWAY)
    pinged: list[str] = []

    async def ping(self, model):
        pinged.append(model)

    monkeypatch.setattr(AnthropicProvider, "ping", ping)
    data = (await client.post("/api/llm/status/check", headers=headers)).json()
    assert data["ok"] is True and data["error"] is None and data["latency_ms"] >= 1
    assert pinged == ["claude-opus-5"]


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (_status_error(anthropic.AuthenticationError, 401), "auth_failed"),
        (_status_error(anthropic.PermissionDeniedError, 403), "auth_failed"),
        (_status_error(anthropic.NotFoundError, 404), "model_not_found"),
        (_status_error(anthropic.RateLimitError, 429), "rate_limited"),
        (
            anthropic.APIConnectionError(
                request=httpx2.Request("POST", "http://lab.test/v1/messages")
            ),
            "unreachable",
        ),
        (
            anthropic.APITimeoutError(httpx2.Request("POST", "http://lab.test/v1/messages")),
            "unreachable",
        ),
    ],
)
async def test_check_turns_sdk_errors_into_plain_codes(
    client, claude_settings, monkeypatch, exc, code
):
    headers = await _auth(client)
    claude_settings(**GATEWAY)

    async def ping(self, model):
        raise exc

    monkeypatch.setattr(AnthropicProvider, "ping", ping)
    resp = await client.post("/api/llm/status/check", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": False, "latency_ms": None, "error": code, "detail": None}


async def test_check_passes_other_failures_through_without_the_key(
    client, claude_settings, monkeypatch
):
    headers = await _auth(client)
    claude_settings(LITELLM_API_KEY="sk-very-secret", LITELLM_API_BASE="http://lab.test")

    async def ping(self, model):
        raise _status_error(
            anthropic.InternalServerError, 503, "no accounts for key sk-very-secret"
        )

    monkeypatch.setattr(AnthropicProvider, "ping", ping)
    data = (await client.post("/api/llm/status/check", headers=headers)).json()
    assert data["ok"] is False and data["error"] == "failed"
    assert data["detail"].startswith("HTTP 503") and "sk-very-secret" not in data["detail"]


async def test_removed_configuration_endpoints_are_gone(client):
    headers = await _auth(client)
    for method, path in [
        ("get", "/api/admin/llm/providers"),
        ("get", "/api/admin/llm/routes"),
        ("get", "/api/admin/llm/input-budgets"),
        ("post", "/api/admin/llm/test-model"),
        ("get", "/api/acp-agents"),
        ("get", "/api/acp-agents/templates"),
        ("get", "/api/chat/backends"),
    ]:
        resp = await getattr(client, method)(path, headers=headers)
        assert resp.status_code in (404, 405), (path, resp.status_code)
    # 用量与调用日志还在
    assert (await client.get("/api/admin/llm/usage", headers=headers)).status_code == 200
    assert (await client.get("/api/admin/llm/call-logs", headers=headers)).status_code == 200
