"""同时在途的模型请求数（core/llm/concurrency.py）：按网关的反应收紧、放宽。"""

import asyncio

import pytest
from anthropic import APIStatusError

from app.core.llm import concurrency
from app.core.llm.base import Message
from app.core.llm.concurrency import AdaptiveLimit
from app.core.llm.errors import LLMTransportError
from tests.test_claude_provider import MODEL, Gateway, _error, _json, _message


async def test_requests_in_flight_never_exceed_the_limit():
    gate = AdaptiveLimit(3)
    peak = 0

    async def call() -> None:
        nonlocal peak
        async with gate.slot():
            peak = max(peak, gate.in_flight)
            await asyncio.sleep(0.01)

    await asyncio.gather(*(call() for _ in range(10)))
    assert peak == 3 and gate.in_flight == 0


async def test_overload_halves_the_limit_and_successes_win_it_back():
    gate = AdaptiveLimit(8, floor=2, recover_after=3)
    gate.overloaded()
    assert gate.limit == 4
    gate.overloaded()
    gate.overloaded()
    assert gate.limit == 2  # 不低于下限
    for _ in range(3):
        await gate.succeeded()
    assert gate.limit == 3


async def test_a_waiting_request_starts_when_the_limit_grows():
    gate = AdaptiveLimit(2, floor=1, recover_after=1)
    release = asyncio.Event()
    started: list[int] = []

    async def hold(i: int) -> None:
        async with gate.slot():
            started.append(i)
            await release.wait()

    holders = [asyncio.create_task(hold(i)) for i in range(3)]
    await asyncio.sleep(0.01)
    assert started == [0, 1]
    await gate.succeeded()  # 上限加到 3：排队的那个立刻进来
    await asyncio.sleep(0.01)
    assert started == [0, 1, 2]
    release.set()
    await asyncio.gather(*holders)


async def test_the_provider_tightens_when_the_gateway_says_busy():
    """网关回 429 / 503 / 529、流被掐断：同一个接口地址的上限收紧；别的失败不算。"""
    gw = Gateway(_error(429, "rate_limit_error", **{"retry-after-ms": "1"}))
    with pytest.raises(APIStatusError):
        await gw.provider().complete([Message("user", "q")], model=MODEL)
    gate = concurrency.limit_for("anthropic:")
    assert gate.limit == concurrency.START // 2

    bad = Gateway(_error(400, "invalid_request_error"))
    with pytest.raises(APIStatusError):
        await bad.provider().complete([Message("user", "q")], model=MODEL)
    assert gate.limit == concurrency.START // 2

    ok = Gateway(_json(_message([{"type": "text", "text": "ok"}])))
    for _ in range(concurrency.RECOVER_AFTER):
        await ok.provider().complete([Message("user", "q")], model=MODEL)
    assert gate.limit == concurrency.START // 2 + 1
    assert gate.in_flight == 0


def test_cut_streams_and_connection_errors_count_as_busy():
    from app.core.llm.anthropic import _is_overload

    assert _is_overload(LLMTransportError("stream ended"))
    assert not _is_overload(ValueError("bug"))
