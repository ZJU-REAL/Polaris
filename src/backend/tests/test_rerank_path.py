"""rerank 端点路径可配（#810）。

各家没统一：有的开在 ``/rerank``，有的开在 ``/reranks``。写死一个就等于把另一半
本来能用的 reranker 挡在门外，而全局改成后者会把现在能用的 LiteLLM / Cohere 风格
服务一起弄坏——所以差异必须落在配置上，且默认值不能变。
"""

import httpx
import pytest

from app.core.llm.openai_compat import OpenAICompatProvider


def _client(seen: list[str], payload: dict) -> httpx.AsyncClient:
    """记下请求打到哪个 URL，并回一份可控响应。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=payload)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


_COHERE = {"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]}


async def test_the_default_path_is_unchanged():
    """默认必须还是 /rerank：改它就把现在能用的服务弄坏了。"""
    seen: list[str] = []
    provider = OpenAICompatProvider(
        base_url="https://gw.example/v1", api_key="k", client=_client(seen, _COHERE)
    )
    await provider.rerank("q", ["a", "b"], model="m")
    assert seen == ["https://gw.example/v1/rerank"]


async def test_a_configured_path_is_used():
    seen: list[str] = []
    provider = OpenAICompatProvider(
        base_url="https://gw.example/v1",
        api_key="k",
        client=_client(seen, _COHERE),
        rerank_path="/reranks",
    )
    await provider.rerank("q", ["a", "b"], model="m")
    assert seen == ["https://gw.example/v1/reranks"]


async def test_an_empty_path_falls_back_to_the_default():
    """空串是「没配」而不是「拼一个空路径」——后者会打到 base_url 本身。"""
    seen: list[str] = []
    provider = OpenAICompatProvider(
        base_url="https://gw.example/v1", api_key="k", client=_client(seen, _COHERE), rerank_path=""
    )
    await provider.rerank("q", ["a"], model="m")
    assert seen == ["https://gw.example/v1/rerank"]


async def test_scores_are_still_ranked_highest_first():
    """换路径不该顺带改排序语义。"""
    provider = OpenAICompatProvider(
        base_url="https://gw.example/v1", api_key="k", client=_client([], _COHERE)
    )
    result = await provider.rerank("q", ["a", "b"], model="m")
    assert [index for index, _score in result.results] == [1, 0]


async def test_a_wrapped_results_array_is_accepted():
    """有些服务把 results 包在 output 里；两种形状都认。"""
    wrapped = {"output": {"results": [{"index": 0, "relevance_score": 0.5}]}}
    provider = OpenAICompatProvider(
        base_url="https://gw.example/v1", api_key="k", client=_client([], wrapped)
    )
    result = await provider.rerank("q", ["a"], model="m")
    assert result.results == [(0, 0.5)]


async def test_an_unrecognised_shape_names_what_came_back():
    """``data["results"]`` 直接抛 KeyError 时，错误信息里只有 'results' 这个词，
    看不出对面到底回了什么，也看不出该去改哪个设置。"""
    provider = OpenAICompatProvider(
        base_url="https://gw.example/v1",
        api_key="k",
        client=_client([], {"data": [], "detail": "nope"}),
    )
    with pytest.raises(RuntimeError) as excinfo:
        await provider.rerank("q", ["a"], model="m")
    message = str(excinfo.value)
    assert "detail" in message and "data" in message, message
    assert "rerank" in message


async def test_test_connection_uses_the_configured_path():
    """设置页「测试连接」走 _build_provider，不经路由表——它也得用配置的路径。"""
    from app.models.llm_config import LLMProviderConfig
    from app.services.llm_admin import _build_provider

    provider = _build_provider(
        LLMProviderConfig(
            name="gw",
            kind="openai_compat",
            base_url="https://gw.example/v1",
            rerank_path="/reranks",
        )
    )
    assert isinstance(provider, OpenAICompatProvider)
    seen: list[str] = []
    provider._client = _client(seen, _COHERE)
    await provider.rerank("q", ["a", "b"], model="m")
    assert seen == ["https://gw.example/v1/reranks"]
