"""OpenAI Responses API provider（#809）。

用 httpx.MockTransport 截下真实发出的请求、回放服务端的真实形状，断言两件事：
我们发出去的是 Responses 协议（不是换了个 URL 的 Chat Completions），以及收回来的
东西——文本、工具调用、推理摘要、usage——一样不少地落进平台的结构里。
"""

import json

import httpx
import pytest

from app.core.llm.base import (
    Message,
    StreamDone,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolResultBlock,
    ToolUseArgsDelta,
    ToolUseBlock,
    ToolUseStart,
    ToolUseStop,
)
from app.core.llm.openai_responses import OpenAIResponsesProvider

BASE = "https://gw.example/v1"


def _provider(handler) -> OpenAIResponsesProvider:
    return OpenAIResponsesProvider(
        base_url=BASE, api_key="k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


def _json_handler(seen: list[dict], payload: dict, status: int = 200):
    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(status, json=payload)

    return handler


_TEXT_RESPONSE = {
    "model": "gpt-x",
    "status": "completed",
    "output": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking it over"}]},
        {"type": "message", "content": [{"type": "output_text", "text": "Hello."}]},
    ],
    "usage": {
        "input_tokens": 12,
        "output_tokens": 5,
        "total_tokens": 17,
        "output_tokens_details": {"reasoning_tokens": 3},
    },
}


# ---------------------------------------------------------------- 发出去的请求


async def test_it_posts_to_the_responses_endpoint():
    seen: list[dict] = []
    await _provider(_json_handler(seen, _TEXT_RESPONSE)).complete(
        [Message("user", "hi")], model="gpt-x"
    )
    assert seen[0]["url"] == f"{BASE}/responses"


async def test_messages_become_input_items_in_order():
    """system 原样保留角色、原位保留顺序：挪进 instructions 会打乱多条 system 的相对位置。"""
    seen: list[dict] = []
    await _provider(_json_handler(seen, _TEXT_RESPONSE)).complete(
        [
            Message("system", "be brief"),
            Message("user", "question"),
            Message("assistant", "earlier answer"),
            Message("user", "follow-up"),
        ],
        model="gpt-x",
    )
    items = seen[0]["body"]["input"]
    assert [i["role"] for i in items] == ["system", "user", "assistant", "user"]
    assert items[1]["content"] == [{"type": "input_text", "text": "question"}]
    # assistant 说过的话是 output_text，不是 input_text
    assert items[2]["content"] == [{"type": "output_text", "text": "earlier answer"}]


async def test_tool_calls_and_results_are_top_level_items():
    """Responses 里工具调用和结果不属于任何一条消息，是独立的顶层项。"""
    seen: list[dict] = []
    await _provider(_json_handler(seen, _TEXT_RESPONSE)).complete(
        [
            Message("user", "search it"),
            Message(
                "assistant",
                [TextBlock("let me look"), ToolUseBlock("call_1", "search", {"q": "x"})],
            ),
            Message("user", [ToolResultBlock("call_1", '{"hits": 3}')]),
        ],
        model="gpt-x",
    )
    items = seen[0]["body"]["input"]
    assert items[1] == {
        "role": "assistant",
        "content": [{"type": "output_text", "text": "let me look"}],
    }
    assert items[2] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "search",
        "arguments": '{"q": "x"}',
    }
    assert items[3] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": '{"hits": 3}',
    }


async def test_replayed_thinking_is_dropped_not_forged():
    """回放推理要服务端给的 reasoning 项 id；一段摘要文本回灌不回去，伪造一条更糟。"""
    seen: list[dict] = []
    await _provider(_json_handler(seen, _TEXT_RESPONSE)).complete(
        [Message("assistant", [ThinkingBlock("secret chain"), TextBlock("ok")])], model="gpt-x"
    )
    items = seen[0]["body"]["input"]
    assert "secret chain" not in json.dumps(items)
    assert items == [{"role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}]


async def test_tools_are_flat_not_wrapped_in_function():
    """Chat Completions 包一层 {type, function:{...}}；Responses 是扁平的。发错形状会 400。"""
    seen: list[dict] = []
    await _provider(_json_handler(seen, _TEXT_RESPONSE)).complete(
        [Message("user", "hi")],
        model="gpt-x",
        tools=[{"name": "search", "description": "find", "parameters": {"type": "object"}}],
        tool_choice="auto",
    )
    body = seen[0]["body"]
    assert body["tools"] == [
        {
            "type": "function",
            "name": "search",
            "description": "find",
            "parameters": {"type": "object"},
        }
    ]
    assert body["tool_choice"] == "auto"


async def test_forcing_a_named_tool():
    seen: list[dict] = []
    await _provider(_json_handler(seen, _TEXT_RESPONSE)).complete(
        [Message("user", "hi")],
        model="gpt-x",
        tools=[{"name": "search"}],
        tool_choice="search",
    )
    assert seen[0]["body"]["tool_choice"] == {"type": "function", "name": "search"}


async def test_effort_goes_under_reasoning():
    seen: list[dict] = []
    await _provider(_json_handler(seen, _TEXT_RESPONSE)).complete(
        [Message("user", "hi")], model="gpt-x", effort="high"
    )
    body = seen[0]["body"]
    assert body["reasoning"] == {"effort": "high"}
    assert "reasoning_effort" not in body


async def test_images_attach_to_the_last_user_message():
    seen: list[dict] = []
    await _provider(_json_handler(seen, _TEXT_RESPONSE)).complete(
        [Message("user", "what is this")], model="gpt-x", images=[b"\x89PNG"]
    )
    parts = seen[0]["body"]["input"][-1]["content"]
    assert parts[0] == {"type": "input_text", "text": "what is this"}
    assert parts[1]["type"] == "input_image"
    assert parts[1]["image_url"].startswith("data:image/png;base64,")


# ---------------------------------------------------------------- 收回来的结果


async def test_text_reasoning_and_usage_come_back():
    result = await _provider(_json_handler([], _TEXT_RESPONSE)).complete(
        [Message("user", "hi")], model="gpt-x"
    )
    assert result.content == "Hello."
    assert result.finish_reason == "stop"
    assert ThinkingBlock("thinking it over") in result.blocks


async def test_usage_is_normalised_so_accounting_records_real_tokens():
    """Responses 给 input/output_tokens，记账读 prompt/completion_tokens——不归一的话
    router 会悄悄按 len/4 估，用量统计全是估算值，而且不报错。"""
    result = await _provider(_json_handler([], _TEXT_RESPONSE)).complete(
        [Message("user", "hi")], model="gpt-x"
    )
    assert result.usage["prompt_tokens"] == 12
    assert result.usage["completion_tokens"] == 5
    assert result.usage["total_tokens"] == 17
    # 嵌套明细不是整数，记账不认，丢掉
    assert "output_tokens_details" not in result.usage


async def test_a_function_call_is_a_tool_use_turn():
    """有工具调用就必须是 tool_use：上层靠它决定执行工具再来一轮，漏判就被当成最终回答。"""
    payload = {
        "status": "completed",
        "output": [
            {
                "type": "function_call",
                "call_id": "call_9",
                "name": "search",
                "arguments": '{"q": "y"}',
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    result = await _provider(_json_handler([], payload)).complete(
        [Message("user", "hi")], model="gpt-x"
    )
    assert result.finish_reason == "tool_use"
    assert result.tool_calls == (ToolUseBlock("call_9", "search", {"q": "y"}),)


async def test_hitting_the_output_limit_is_reported():
    payload = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "cut"}]}],
    }
    result = await _provider(_json_handler([], payload)).complete(
        [Message("user", "hi")], model="gpt-x"
    )
    assert result.finish_reason == "max_tokens"


async def test_malformed_tool_arguments_do_not_abort_the_turn():
    payload = {
        "status": "completed",
        "output": [
            {"type": "function_call", "call_id": "c", "name": "s", "arguments": "{not json"}
        ],
    }
    result = await _provider(_json_handler([], payload)).complete(
        [Message("user", "hi")], model="gpt-x"
    )
    assert "__parse_error__" in result.tool_calls[0].input


async def test_a_gateway_that_only_sends_output_text_still_works():
    result = await _provider(
        _json_handler([], {"status": "completed", "output_text": "plain"})
    ).complete([Message("user", "hi")], model="gpt-x")
    assert result.content == "plain"


async def test_an_unsupported_effort_is_dropped_and_retried():
    """配错档位不该打断整个环节：去掉参数重试一次，并记住这个模型不吃它。"""
    seen: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "reasoning" in body:
            return httpx.Response(
                400, json={"error": {"message": "Unsupported parameter: reasoning.effort"}}
            )
        return httpx.Response(200, json=_TEXT_RESPONSE)

    provider = _provider(handler)
    result = await provider.complete([Message("user", "hi")], model="gpt-x", effort="xhigh")
    assert result.content == "Hello."
    assert "reasoning" in seen[0] and "reasoning" not in seen[1]
    # 第二次调用不再发，省掉一次注定失败的往返
    await provider.complete([Message("user", "again")], model="gpt-x", effort="xhigh")
    assert "reasoning" not in seen[2]


async def test_upstream_errors_keep_the_reason():
    with pytest.raises(RuntimeError) as excinfo:
        await _provider(
            _json_handler([], {"error": {"message": "model not found"}}, status=404)
        ).complete([Message("user", "hi")], model="nope")
    assert "model not found" in str(excinfo.value)


# ---------------------------------------------------------------- 流式


def _sse(events: list[dict]) -> bytes:
    lines = []
    for event in events:
        lines.append(f"event: {event['type']}")
        lines.append(f"data: {json.dumps(event)}")
        lines.append("")
    return "\n".join(lines).encode()


def _sse_handler(events: list[dict]):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_sse(events), headers={"content-type": "text/event-stream"}
        )

    return handler


_STREAM = [
    {"type": "response.created", "response": {}},
    {"type": "response.reasoning_summary_text.delta", "delta": "hmm"},
    {"type": "response.output_text.delta", "delta": "Hel"},
    {"type": "response.output_text.delta", "delta": "lo"},
    {
        "type": "response.output_item.added",
        "output_index": 2,
        "item": {"type": "function_call", "call_id": "call_5", "name": "search"},
    },
    {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"q":'},
    {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '"z"}'},
    {"type": "response.output_item.done", "output_index": 2, "item": {"type": "function_call"}},
    {
        "type": "response.completed",
        "response": {"status": "completed", "usage": {"input_tokens": 7, "output_tokens": 4}},
    },
]


async def test_stream_events_translate_every_kind():
    events = [
        e
        async for e in _provider(_sse_handler(_STREAM)).stream_events(
            [Message("user", "hi")], model="gpt-x", tools=[{"name": "search"}]
        )
    ]
    assert events[0] == ThinkingDelta("hmm")
    assert events[1:3] == [TextDelta("Hel"), TextDelta("lo")]
    assert events[3] == ToolUseStart(2, "call_5", "search")
    assert events[4:6] == [ToolUseArgsDelta(2, '{"q":'), ToolUseArgsDelta(2, '"z"}')]
    assert events[6] == ToolUseStop(2)
    done = events[7]
    assert isinstance(done, StreamDone)
    assert done.finish_reason == "tool_use"
    assert done.usage["prompt_tokens"] == 7 and done.usage["completion_tokens"] == 4


async def test_plain_stream_yields_only_text():
    chunks = [
        c
        async for c in _provider(_sse_handler(_STREAM)).stream(
            [Message("user", "hi")], model="gpt-x"
        )
    ]
    assert chunks == ["Hel", "lo"]


async def test_a_truncated_stream_still_finishes():
    """连接正常关掉却没给 response.completed：如实收尾，别让上层永远等 StreamDone。"""
    truncated = [{"type": "response.output_text.delta", "delta": "partial"}]
    events = [
        e
        async for e in _provider(_sse_handler(truncated)).stream_events(
            [Message("user", "hi")], model="gpt-x"
        )
    ]
    assert events == [TextDelta("partial"), StreamDone(finish_reason="stop")]


async def test_a_failed_stream_raises_with_the_reason():
    failed = [{"type": "response.failed", "response": {"error": {"message": "rate limited"}}}]
    with pytest.raises(RuntimeError) as excinfo:
        async for _ in _provider(_sse_handler(failed)).stream_events(
            [Message("user", "hi")], model="gpt-x"
        ):
            pass
    assert "rate limited" in str(excinfo.value)


async def test_a_force_stream_gateway_is_aggregated():
    """强制流式的网关对非流式请求回 400；自动改走流式并聚合成一次性结果。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if not body.get("stream"):
            return httpx.Response(400, json={"error": {"message": "stream must be set to true"}})
        return httpx.Response(
            200, content=_sse(_STREAM), headers={"content-type": "text/event-stream"}
        )

    result = await _provider(handler).complete(
        [Message("user", "hi")], model="gpt-x", tools=[{"name": "search"}]
    )
    assert result.content == "Hello"
    assert result.tool_calls == (ToolUseBlock("call_5", "search", {"q": "z"}),)
    assert result.finish_reason == "tool_use"


# ---------------------------------------------------------------- 接线


def test_the_router_builds_a_responses_provider():
    from app.core.llm.router import LLMRouter, ResolvedRoute

    route = ResolvedRoute(
        provider_kind="openai_responses",
        base_url=BASE,
        api_key="k",
        model="gpt-x",
        temperature=None,
    )
    assert isinstance(LLMRouter()._provider_for(route, "default"), OpenAIResponsesProvider)


def test_the_connection_test_uses_the_real_protocol():
    """「测试连接」以前对没认出来的类型回退 FakeProvider：探一个假的、报告成功，
    真实服务一次都没碰过。"""
    from app.models.llm_config import LLMProviderConfig
    from app.services.llm_admin import _build_provider

    cfg = LLMProviderConfig(name="r", kind="openai_responses", base_url=BASE, enabled=True)
    assert isinstance(_build_provider(cfg), OpenAIResponsesProvider)


def test_an_unknown_kind_is_refused_rather_than_faked():
    from app.models.llm_config import LLMProviderConfig
    from app.services.llm_admin import _build_provider

    cfg = LLMProviderConfig(name="x", kind="not_a_protocol", enabled=True)
    with pytest.raises(ValueError):
        _build_provider(cfg)


def test_embeddings_and_rerank_are_inherited():
    """同属 OpenAI 一族：/embeddings 与 rerank 走同一套实现，一个网关配一个 provider 就够。"""
    from app.core.llm.openai_compat import OpenAICompatProvider

    assert OpenAIResponsesProvider.embed is OpenAICompatProvider.embed
    assert OpenAIResponsesProvider.rerank is OpenAICompatProvider.rerank


def test_rerank_path_reaches_responses_providers():
    """rerank 继承自 compat provider（#810 的路径配置）：路由器和「测试连接」都得把它带上。"""
    from app.core.llm.router import LLMRouter, ResolvedRoute
    from app.models.llm_config import LLMProviderConfig
    from app.services.llm_admin import _build_provider

    route = ResolvedRoute(
        provider_kind="openai_responses",
        base_url="https://gw.example/v1",
        api_key="k",
        model="m",
        temperature=None,
        rerank_path="/reranks",
    )
    assert LLMRouter()._provider_for(route, "rerank")._rerank_path == "/reranks"

    built = _build_provider(
        LLMProviderConfig(
            name="gw",
            kind="openai_responses",
            base_url="https://gw.example/v1",
            rerank_path="/reranks",
        )
    )
    assert built._rerank_path == "/reranks"
