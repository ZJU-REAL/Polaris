"""费用与用量的口径：缓存折扣、流式调用的真实用量、开始前的估算。"""

import uuid

import pytest
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.llm import pricing
from app.core.llm.base import LLMProvider, StreamDone, TextDelta
from app.core.llm.errors import LLMAuthError, LLMTransportError
from app.core.llm.router import LLMRouter, ResolvedRoute
from app.models.llm_config import LLMUsage
from app.models.voyage import VoyageRun
from app.services import research_estimate
from app.services import research_runs as research_service
from tests.conftest import register_and_login

OPUS = "claude-opus-5"


def test_cache_reads_are_a_tenth_and_writes_a_quarter_more():
    """$5 / $25 每百万 token；读缓存 0.1 倍、写缓存 1.25 倍。"""
    full = pricing.cost_usd(model=OPUS, prompt_tokens=1_000_000, completion_tokens=0)
    assert full == pytest.approx(5.0)
    assert pricing.cost_usd(
        model=OPUS, prompt_tokens=0, completion_tokens=1_000_000
    ) == pytest.approx(25.0)
    read = pricing.cost_usd(
        model=OPUS, prompt_tokens=1_000_000, completion_tokens=0, cache_read_tokens=1_000_000
    )
    assert read == pytest.approx(0.5)
    write = pricing.cost_usd(
        model=OPUS, prompt_tokens=1_000_000, completion_tokens=0, cache_write_tokens=1_000_000
    )
    assert write == pytest.approx(6.25)
    # 网关风格的前缀照样认得
    assert pricing.price_for(f"anthropic/{OPUS}") == pricing.PRICES[OPUS]


def test_embeddings_and_rerank_are_free_unless_priced():
    assert (
        pricing.cost_usd(
            model="bge-m3", stage="embedding", prompt_tokens=10**6, completion_tokens=0
        )
        == 0
    )
    assert (
        pricing.cost_usd(
            model="some-reranker", stage="rerank", prompt_tokens=10**6, completion_tokens=0
        )
        == 0
    )
    # 认不出名字的对话模型按平台唯一接的那个模型计，不当成免费
    assert pricing.cost_usd(
        model="gateway-alias", stage="hyp_ground", prompt_tokens=10**6, completion_tokens=0
    ) == pytest.approx(5.0)


async def _voyage_with_usage(rows: list[dict]) -> uuid.UUID:
    async with get_sessionmaker()() as session:
        voyage = VoyageRun(kind="discovery", goal="g", status="done")
        session.add(voyage)
        await session.flush()
        for row in rows:
            session.add(LLMUsage(voyage_id=voyage.id, **row))
        await session.commit()
        return voyage.id


async def test_router_records_cache_tokens_separately(client):
    router = LLMRouter()
    await router._record_usage(
        stage="hyp_novelty",
        model=OPUS,
        usage={
            "prompt_tokens": 20_000,
            "completion_tokens": 300,
            "cache_read_input_tokens": 18_000,
            "cache_creation_input_tokens": 500,
        },
        user_id=None,
        project_id=None,
        voyage_id=None,
    )
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(LLMUsage).where(LLMUsage.stage == "hyp_novelty"))
        ).scalar_one()
    assert (row.prompt_tokens, row.cache_read_tokens, row.cache_write_tokens) == (
        20_000,
        18_000,
        500,
    )


async def test_run_usage_applies_the_cache_discount_and_skips_embeddings(client):
    voyage_id = await _voyage_with_usage(
        [
            # 接地：写缓存
            dict(
                stage="hyp_ground",
                model=OPUS,
                prompt_tokens=100_000,
                completion_tokens=1_000,
                cache_write_tokens=90_000,
            ),
            # 查新：读缓存
            dict(
                stage="hyp_novelty",
                model=OPUS,
                prompt_tokens=100_000,
                completion_tokens=1_000,
                cache_read_tokens=90_000,
            ),
            # 向量化：不计价，也不算进对话模型的 token
            dict(stage="embedding", model="bge-m3", prompt_tokens=500_000, completion_tokens=0),
        ]
    )
    async with get_sessionmaker()() as session:
        voyage = await session.get(VoyageRun, voyage_id)
        usage = await research_service._usage(session, voyage, None)
    assert usage.prompt_tokens == 200_000 and usage.completion_tokens == 2_000
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (90_000, 90_000)
    # (10k + 90k×1.25) + (10k + 90k×0.1) = 141.5k 输入当量 × $5 + 2k × $25
    assert usage.cost_usd == pytest.approx(round(141_500 * 5 / 1e6 + 2_000 * 25 / 1e6, 2))
    assert usage.cost_approximate is True
    # 不计折扣的旧算法是 (700k × 5 + 2k × 25) / 1e6 = 3.55
    assert usage.cost_usd < 1.0


def test_estimate_scales_with_the_survey_size():
    small = research_estimate.estimate_from(
        research_estimate.DEFAULTS, max_papers=60, compile_top_n=20
    )
    large = research_estimate.estimate_from(
        research_estimate.DEFAULTS, max_papers=200, compile_top_n=60
    )
    for key in ("tokens", "cost_usd", "minutes"):
        assert small[f"{key}_low"] < small[f"{key}_high"]
        assert small[f"{key}_high"] < large[f"{key}_high"]
    # 解读篇数不会超过筛选篇数
    assert research_estimate.estimate_from(
        research_estimate.DEFAULTS, max_papers=10, compile_top_n=200
    ) == research_estimate.estimate_from(
        research_estimate.DEFAULTS, max_papers=10, compile_top_n=10
    )


async def test_estimate_endpoint_has_defaults_without_history(client):
    token = await register_and_login(client)
    headers = {"Authorization": f"Bearer {token}"}
    resp = await client.get(
        "/api/research/estimate?max_papers=60&compile_top_n=20", headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["max_papers"], body["compile_top_n"]) == (60, 20)
    assert body["based_on_runs"] == 0 and body["approximate"] is True
    assert 0 < body["cost_usd_low"] < body["cost_usd_high"]
    assert 0 < body["minutes_low"] < body["minutes_high"]
    assert 0 < body["tokens_low"] < body["tokens_high"]
    # 不带参数 = 产品默认规模（200 / 60），比小规模贵
    default = (await client.get("/api/research/estimate", headers=headers)).json()
    assert (default["max_papers"], default["compile_top_n"]) == (200, 60)
    assert default["cost_usd_high"] > body["cost_usd_high"]
    assert (
        await client.get("/api/research/estimate?max_papers=1", headers=headers)
    ).status_code == 422


# ---- 流式调用：真实用量 + 流被掐断时整段重试 ----


class _Bus:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def publish_voyage_event(self, _voyage_id, kind, _payload) -> None:
        self.events.append(kind)


class _CutStream(LLMProvider):
    """前 ``cuts`` 次流到一半断掉（收不到收尾事件），之后正常收尾并报用量。"""

    name = "cut"

    def __init__(self, cuts: int, error: Exception | None = None) -> None:
        self.cuts = cuts
        self.calls = 0
        self.error = error or LLMTransportError("model stream ended before the final message")

    async def complete(self, messages, **kwargs):  # pragma: no cover — 不走这里
        raise AssertionError("streaming stage must stream")

    async def stream(self, messages, **kwargs):  # pragma: no cover — 不走这里
        raise AssertionError("usage comes from stream_events")
        yield ""

    async def stream_events(self, messages, **kwargs):
        self.calls += 1
        yield TextDelta("半截")
        if self.calls <= self.cuts:
            raise self.error
        yield TextDelta("正文")
        yield StreamDone(
            finish_reason="stop",
            usage={
                "prompt_tokens": 30_000,
                "completion_tokens": 4_200,
                "cache_read_input_tokens": 2_000,
            },
        )


async def _stream(provider: LLMProvider, monkeypatch) -> tuple[object, _Bus]:
    from app.core.llm import router as router_module

    monkeypatch.setattr(router_module, "_STREAM_RETRY_BASE_SECONDS", 0.0)
    router = LLMRouter()
    bus = _Bus()
    router.event_bus = bus
    route = ResolvedRoute(provider_kind="fake", base_url=None, api_key="", model=OPUS)
    result = await router._stream_and_broadcast(
        "librarian", provider, route, [], 0.0, None, uuid.uuid4()
    )
    return result, bus


async def test_a_cut_stream_is_retried_up_to_three_times(client, monkeypatch):
    """网关把流中途掐断：整段重试，第三次重试成功就不丢这篇解读。"""
    provider = _CutStream(cuts=3)
    result, bus = await _stream(provider, monkeypatch)
    assert provider.calls == 4 and result.content == "半截正文"  # 只留最后一次完整的输出
    assert bus.events.count("llm_start") == 4
    # 用量是服务端报的（含思考与缓存），不是按正文长度估的
    assert result.usage == {
        "prompt_tokens": 30_000,
        "completion_tokens": 4_200,
        "cache_read_input_tokens": 2_000,
    }


async def test_a_stream_that_keeps_failing_raises_after_the_retries(client, monkeypatch):
    provider = _CutStream(cuts=99)
    with pytest.raises(LLMTransportError):
        await _stream(provider, monkeypatch)
    assert provider.calls == 4


async def test_a_non_transient_stream_failure_is_not_retried(client, monkeypatch):
    provider = _CutStream(cuts=99, error=LLMAuthError("HTTP 401"))
    with pytest.raises(LLMAuthError):
        await _stream(provider, monkeypatch)
    assert provider.calls == 1
