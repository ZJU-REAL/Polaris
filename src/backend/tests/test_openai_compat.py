"""openai_compat 强制流式回退测试：非流式 400 "Stream must be set to true"
→ 自动改用流式请求并聚合（content 拼接 / usage 取带 usage 的 chunk）；
其他 400 不触发回退。respx mock。"""

import json

import httpx
import pytest
import respx

from app.core.llm.base import Message
from app.core.llm.openai_compat import OpenAICompatProvider

BASE_URL = "http://relay.test/api/v1"
CHAT_URL = f"{BASE_URL}/chat/completions"


def _sse(chunks: list[dict]) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


STREAM_CHUNKS = [
    {
        "id": "c1",
        "model": "gpt-5.6-sol",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hel"}}],
    },
    {"id": "c1", "model": "gpt-5.6-sol", "choices": [{"index": 0, "delta": {"content": "lo "}}]},
    {
        "id": "c1",
        "model": "gpt-5.6-sol",
        "choices": [{"index": 0, "delta": {"content": "world"}, "finish_reason": "stop"}],
    },
    {
        "id": "c1",
        "model": "gpt-5.6-sol",
        "choices": [],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
    },
]

FORCE_STREAM_400 = httpx.Response(400, json={"detail": "Stream must be set to true"})


def _stream_response() -> httpx.Response:
    return httpx.Response(
        200, content=_sse(STREAM_CHUNKS), headers={"content-type": "text/event-stream"}
    )


@respx.mock
async def test_complete_force_stream_fallback_aggregates():
    route = respx.post(CHAT_URL).mock(side_effect=[FORCE_STREAM_400, _stream_response()])
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    result = await provider.complete([Message(role="user", content="hi")], model="gpt-5.6-sol")

    assert result.content == "Hello world"
    assert result.model == "gpt-5.6-sol"
    assert result.finish_reason == "stop"
    assert result.usage == {"prompt_tokens": 12, "completion_tokens": 5}

    assert route.call_count == 2
    first = json.loads(route.calls[0].request.content)
    second = json.loads(route.calls[1].request.content)
    assert first["stream"] is False
    assert second["stream"] is True
    assert second["messages"] == first["messages"]  # 除 stream 外 payload 不变
    await provider.aclose()


@respx.mock
async def test_complete_force_stream_fallback_no_usage_chunk():
    """带 usage 的 chunk 缺失时 usage 为空（记账按 0 处理）。"""
    respx.post(CHAT_URL).mock(
        side_effect=[
            FORCE_STREAM_400,
            httpx.Response(
                200,
                content=_sse([c for c in STREAM_CHUNKS if "usage" not in c]),
                headers={"content-type": "text/event-stream"},
            ),
        ]
    )
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    result = await provider.complete([Message(role="user", content="hi")], model="gpt-5.6-sol")
    assert result.content == "Hello world"
    assert result.usage == {}
    assert result.usage.get("prompt_tokens", 0) == 0
    await provider.aclose()


@respx.mock
async def test_complete_force_stream_fallback_with_images():
    """多模态 images 分支同样回退：流式重试的 payload 保留 image_url parts。"""
    route = respx.post(CHAT_URL).mock(side_effect=[FORCE_STREAM_400, _stream_response()])
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    result = await provider.complete(
        [Message(role="user", content="describe")],
        model="gpt-5.6-sol",
        images=[b"\x89PNG-fake"],
    )
    assert result.content == "Hello world"
    assert route.call_count == 2
    second = json.loads(route.calls[1].request.content)
    assert second["stream"] is True
    parts = second["messages"][-1]["content"]
    assert parts[0] == {"type": "text", "text": "describe"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    await provider.aclose()


@respx.mock
async def test_complete_other_400_not_retried_as_stream():
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(400, json={"detail": "model not found"})
    )
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    with pytest.raises(RuntimeError, match="openai_compat 400"):
        await provider.complete([Message(role="user", content="hi")], model="nope")
    assert route.call_count == 1  # 未触发流式回退
    await provider.aclose()


@respx.mock
async def test_stream_still_yields_deltas():
    """stream() 复用同一份 SSE 解析：空 choices 的 usage chunk 被跳过。"""
    respx.post(CHAT_URL).mock(return_value=_stream_response())
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    got = [
        c async for c in provider.stream([Message(role="user", content="hi")], model="gpt-5.6-sol")
    ]
    assert "".join(got) == "Hello world"
    await provider.aclose()
