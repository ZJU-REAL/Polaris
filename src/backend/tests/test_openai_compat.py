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
EMBEDDING_URL = f"{BASE_URL}/embeddings"


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

# 非流式成功响应（effort 透传用例不关心内容，只查 payload）
NON_STREAM_OK = {
    "id": "c2",
    "model": "gpt-5.6-sol",
    "choices": [
        {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}


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


@respx.mock
async def test_effort_sent_as_reasoning_effort():
    """route 上配了 effort 时，payload 带 reasoning_effort（codex-relay 等推理中转认这个字段）。"""
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=NON_STREAM_OK))
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    await provider.complete(
        [Message(role="user", content="hi")], model="gpt-5.6-sol", effort="xhigh"
    )
    body = json.loads(route.calls[0].request.content)
    assert body["reasoning_effort"] == "xhigh"
    await provider.aclose()


@respx.mock
async def test_qwen3_embedding_requests_1024_dimensions():
    route = respx.post(EMBEDDING_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 1024}]})
    )
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")

    vectors = await provider.embed(["hello"], model="qwen3_embedding_8b")

    assert len(vectors[0]) == 1024
    payload = json.loads(route.calls.last.request.content)
    assert payload["dimensions"] == 1024
    await provider.aclose()


@respx.mock
async def test_effort_omitted_when_unset():
    """不配 effort 就完全不发该字段——非推理模型/老中转收到会 400。"""
    route = respx.post(CHAT_URL).mock(return_value=httpx.Response(200, json=NON_STREAM_OK))
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    await provider.complete([Message(role="user", content="hi")], model="gpt-5.6-sol")
    assert "reasoning_effort" not in json.loads(route.calls[0].request.content)
    await provider.aclose()


@respx.mock
async def test_effort_survives_force_stream_fallback():
    """强制流式的中转（codex-relay 就是）：回退重试时 effort 不能丢。"""
    route = respx.post(CHAT_URL).mock(side_effect=[FORCE_STREAM_400, _stream_response()])
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    result = await provider.complete(
        [Message(role="user", content="hi")], model="gpt-5.6-sol", effort="low"
    )
    assert result.content == "Hello world"
    second = json.loads(route.calls[1].request.content)
    assert second["stream"] is True
    assert second["reasoning_effort"] == "low"
    await provider.aclose()


@respx.mock
async def test_effort_sent_on_stream():
    route = respx.post(CHAT_URL).mock(return_value=_stream_response())
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    chunks = [
        c
        async for c in provider.stream(
            [Message(role="user", content="hi")], model="gpt-5.6-sol", effort="high"
        )
    ]
    assert "".join(chunks) == "Hello world"
    assert json.loads(route.calls[0].request.content)["reasoning_effort"] == "high"
    await provider.aclose()


@respx.mock
async def test_effort_rejected_falls_back_without_it():
    """模型不支持所配档位时去掉 effort 重试一次，环节不挂。"""
    reject = httpx.Response(
        400,
        json={
            "error": {
                "message": "Unsupported value: 'xhigh' is not supported with the 'gpt-4o' model.",
                "param": "reasoning.effort",
            }
        },
    )
    route = respx.post(CHAT_URL).mock(side_effect=[reject, httpx.Response(200, json=NON_STREAM_OK)])
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    result = await provider.complete(
        [Message(role="user", content="hi")], model="gpt-4o", effort="xhigh"
    )
    assert result.content == "ok"
    assert route.call_count == 2
    assert json.loads(route.calls[0].request.content)["reasoning_effort"] == "xhigh"
    assert "reasoning_effort" not in json.loads(route.calls[1].request.content)
    await provider.aclose()


@respx.mock
async def test_qwen3_embedding_falls_back_for_old_vllm_and_normalizes():
    native = [3.0, 4.0, *([0.0] * 4094)]
    route = respx.post(EMBEDDING_URL).mock(
        side_effect=[
            httpx.Response(
                400,
                json={
                    "error": {
                        "message": (
                            'Model "qwen3_embedding_8b" does not support matryoshka '
                            "representation, changing output dimensions will lead to poor results."
                        )
                    }
                },
            ),
            httpx.Response(200, json={"data": [{"index": 0, "embedding": native}]}),
        ]
    )
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")

    vectors = await provider.embed(["hello"], model="qwen3_embedding_8b")

    assert route.call_count == 2
    assert json.loads(route.calls[0].request.content)["dimensions"] == 1024
    assert "dimensions" not in json.loads(route.calls[1].request.content)
    assert len(vectors[0]) == 1024
    assert vectors[0][:2] == pytest.approx([0.6, 0.8])
    assert sum(value * value for value in vectors[0]) == pytest.approx(1.0)
    await provider.aclose()


@respx.mock
async def test_litellm_unsupported_params_falls_back_regardless_of_status():
    """LiteLLM 把 UnsupportedParamsError 包成非 400 抛出时，仍要认出是 effort 的问题。

    现网实例（qwen 系走 LiteLLM 中转）报的就是这一条：判据若卡在 400 上，
    整个环节直接挂掉。所以只认错误内容、不认状态码。
    """
    body = {
        "error": {
            "message": (
                "litellm.UnsupportedParamsError: openai does not support parameters: "
                "['reasoning_effort'], for model=qwen36-35b-a3b-4. To drop these, set "
                "`litellm.drop_params=True`. Received Model Group=qwen36-35b-a3b"
            )
        }
    }
    route = respx.post(CHAT_URL).mock(
        side_effect=[httpx.Response(500, json=body), httpx.Response(200, json=NON_STREAM_OK)]
    )
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    result = await provider.complete(
        [Message(role="user", content="hi")], model="qwen36-35b-a3b", effort="high"
    )
    assert result.content == "ok"
    assert "reasoning_effort" not in json.loads(route.calls[-1].request.content)
    await provider.aclose()


@respx.mock
async def test_effort_unsupported_is_remembered_per_model():
    """同一 model 只付一次「先失败再重试」的往返，后续请求直接不带该参数。"""
    reject = httpx.Response(400, json={"error": {"message": "unsupported reasoning_effort"}})
    ok = httpx.Response(200, json=NON_STREAM_OK)
    route = respx.post(CHAT_URL).mock(side_effect=[reject, ok, ok])
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    args = ([Message(role="user", content="hi")],)
    await provider.complete(*args, model="qwen36-35b-a3b", effort="high")
    await provider.complete(*args, model="qwen36-35b-a3b", effort="high")
    assert route.call_count == 3  # 首次 2 次（含重试），第二次只 1 次
    assert "reasoning_effort" not in json.loads(route.calls[-1].request.content)
    await provider.aclose()


@respx.mock
async def test_unrelated_400_is_not_swallowed_as_effort_problem():
    """与 effort 无关的 400 照常抛错，不能被降级逻辑吞掉。"""
    route = respx.post(CHAT_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "model not found"}})
    )
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    with pytest.raises(RuntimeError, match="model not found"):
        await provider.complete([Message(role="user", content="hi")], model="nope", effort="high")
    assert route.call_count == 1  # 没有重试
    await provider.aclose()


@respx.mock
async def test_effort_rejected_on_forced_stream_relay():
    """强制流式 + 不支持该档位：先回退流式，再去掉 effort，最终仍拿到结果。"""
    reject = httpx.Response(400, json={"error": {"message": "unsupported reasoning_effort"}})
    route = respx.post(CHAT_URL).mock(
        side_effect=[FORCE_STREAM_400, reject, FORCE_STREAM_400, _stream_response()]
    )
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    result = await provider.complete(
        [Message(role="user", content="hi")], model="gpt-5.6-sol", effort="max"
    )
    assert result.content == "Hello world"
    assert "reasoning_effort" not in json.loads(route.calls[-1].request.content)
    await provider.aclose()


@respx.mock
async def test_effort_rejected_on_stream_retries_without_it():
    reject = httpx.Response(400, json={"error": {"message": "unsupported reasoning_effort"}})
    route = respx.post(CHAT_URL).mock(side_effect=[reject, _stream_response()])
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")
    chunks = [
        c
        async for c in provider.stream(
            [Message(role="user", content="hi")], model="gpt-4o", effort="xhigh"
        )
    ]
    assert "".join(chunks) == "Hello world"
    assert route.call_count == 2
    assert "reasoning_effort" not in json.loads(route.calls[1].request.content)
    await provider.aclose()


@respx.mock
async def test_non_qwen_embedding_request_is_unchanged():
    route = respx.post(EMBEDDING_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1] * 1024}]})
    )
    provider = OpenAICompatProvider(base_url=BASE_URL, api_key="sk-test")

    vectors = await provider.embed(["hello"], model="bge-m3")

    assert len(vectors[0]) == 1024
    assert "dimensions" not in json.loads(route.calls.last.request.content)
    await provider.aclose()
