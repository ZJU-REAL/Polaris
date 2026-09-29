"""Anthropic provider 的退避重试（#821 E1）。

以前一次 429 或 529（Anthropic 的「服务过载」，高峰期常见）就让整个环节失败，
而 OpenAI 兼容 provider 早就会退避重试。
"""

import json

import httpx
import pytest

from app.core.llm import anthropic as anthropic_mod
from app.core.llm.anthropic import AnthropicProvider
from app.core.llm.base import Message, StreamDone, TextDelta

OK_BODY = {
    "content": [{"type": "text", "text": "hi"}],
    "model": "claude-x",
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 3, "output_tokens": 1},
}
MSG = [Message(role="user", content="ping")]


@pytest.fixture
def slept(monkeypatch):
    delays: list[float] = []

    async def _sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(anthropic_mod.asyncio, "sleep", _sleep)
    return delays


def _provider(responses, calls):
    it = iter(responses)

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item

    return AnthropicProvider(
        api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


async def test_an_overloaded_529_is_retried(slept):
    calls: list = []
    prov = _provider([httpx.Response(529), httpx.Response(200, json=OK_BODY)], calls)
    result = await prov.complete(MSG, model="claude-x")
    assert result.content == "hi"
    assert len(calls) == 2
    assert len(slept) == 1


async def test_retry_after_is_honoured(slept):
    calls: list = []
    prov = _provider(
        [httpx.Response(429, headers={"retry-after": "7"}), httpx.Response(200, json=OK_BODY)],
        calls,
    )
    await prov.complete(MSG, model="claude-x")
    assert slept == [7.0]


async def test_a_network_error_is_retried(slept):
    calls: list = []
    prov = _provider([httpx.ConnectError("boom"), httpx.Response(200, json=OK_BODY)], calls)
    assert (await prov.complete(MSG, model="claude-x")).content == "hi"
    assert len(calls) == 2


async def test_a_client_error_is_not_retried(slept):
    calls: list = []
    prov = _provider([httpx.Response(400, json={"error": "bad request"})], calls)
    with pytest.raises(httpx.HTTPStatusError):
        await prov.complete(MSG, model="claude-x")
    assert len(calls) == 1
    assert slept == []


async def test_retries_are_bounded(slept):
    calls: list = []
    prov = _provider([httpx.Response(529)] * 3, calls)
    with pytest.raises(httpx.HTTPStatusError):
        await prov.complete(MSG, model="claude-x")
    assert len(calls) == 3


async def test_an_effort_rejection_still_falls_back_without_backoff(slept):
    """「不支持 effort」不是临时故障：不该退避重试，直接去掉参数再发一次。"""
    calls: list = []
    prov = _provider(
        [
            httpx.Response(400, json={"error": {"message": "output_config.effort not supported"}}),
            httpx.Response(200, json=OK_BODY),
        ],
        calls,
    )
    result = await prov.complete(MSG, model="claude-x", effort="high")
    assert result.content == "hi"
    assert "output_config" in calls[0] and "output_config" not in calls[1]
    assert slept == []


def _sse(*events: dict) -> str:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


STREAM_OK = _sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 2}}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "yo"}},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
)


async def test_a_stream_that_fails_to_open_is_retried(slept):
    calls: list = []
    prov = _provider(
        [httpx.Response(529), httpx.Response(200, text=STREAM_OK)],
        calls,
    )
    events = [ev async for ev in prov.stream_events(MSG, model="claude-x")]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["yo"]
    assert isinstance(events[-1], StreamDone)
    assert len(calls) == 2


async def test_a_stream_connection_error_before_any_event_is_retried(slept):
    calls: list = []
    prov = _provider([httpx.ConnectError("boom"), httpx.Response(200, text=STREAM_OK)], calls)
    text = [
        e.text async for e in prov.stream_events(MSG, model="claude-x") if isinstance(e, TextDelta)
    ]
    assert text == ["yo"]
