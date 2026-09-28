"""``/embeddings`` 响应的各种形状（#819）。

「OpenAI 兼容」的服务在嵌入端点上不都回 OpenAI 格式。llama.cpp 的 Base URL 没写
``/v1`` 时打到的是它的原生端点：顶层是列表、每条向量还多包一层。以前只认
``{"data": [...]}``，于是本地 llama.cpp 的嵌入在路由表测试里一律失败。
"""

import httpx
import pytest

from app.core.llm.openai_compat import OpenAICompatProvider


def _provider(payload) -> OpenAICompatProvider:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return OpenAICompatProvider(
        base_url="http://llama:8082",
        api_key="k",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_openai_shape_is_unchanged():
    body = {
        "data": [
            {"index": 1, "embedding": [0.3, 0.4]},
            {"index": 0, "embedding": [0.1, 0.2]},
        ]
    }
    assert await _provider(body).embed(["a", "b"], model="m") == [[0.1, 0.2], [0.3, 0.4]]


async def test_llama_cpp_native_endpoint():
    """issue 里的现场：llama.cpp 原生 /embeddings，顶层列表 + 每条 [[...]]。"""
    body = [
        {"index": 0, "embedding": [[0.1, 0.2, 0.3]]},
        {"index": 1, "embedding": [[0.4, 0.5, 0.6]]},
    ]
    assert await _provider(body).embed(["a", "b"], model="m") == [
        [0.1, 0.2, 0.3],
        [0.4, 0.5, 0.6],
    ]


async def test_bare_vector_list():
    assert await _provider([[1, 2], [3, 4]]).embed(["a", "b"], model="m") == [
        [1.0, 2.0],
        [3.0, 4.0],
    ]


async def test_embeddings_key():
    assert await _provider({"embeddings": [[0.5, 0.5]]}).embed(["a"], model="m") == [[0.5, 0.5]]


async def test_per_token_output_is_refused_not_averaged():
    """没开池化时按 token 返回——那不是句向量，不能悄悄平均出一个来用。"""
    body = [{"index": 0, "embedding": [[0.1, 0.2], [0.3, 0.4]]}]
    with pytest.raises(ValueError, match="pooling"):
        await _provider(body).embed(["a"], model="m")


async def test_count_mismatch_is_refused():
    with pytest.raises(ValueError, match="1 vectors for 2 inputs"):
        await _provider({"data": [{"index": 0, "embedding": [0.1]}]}).embed(["a", "b"], model="m")


async def test_unknown_shape_names_what_came_back():
    with pytest.raises(ValueError, match="keys=\\['error', 'object'\\]"):
        await _provider({"object": "list", "error": "x"}).embed(["a"], model="m")


async def test_non_numeric_vector_is_refused():
    with pytest.raises(ValueError, match="non-numeric"):
        await _provider({"data": [{"index": 0, "embedding": "abc"}]}).embed(["a"], model="m")
