"""Claude provider（core/llm/anthropic.py）：请求怎么拼、响应怎么读。

走的是真的官方 SDK，只把最底下的 HTTP 换成 MockTransport——所以这里验的是
「SDK 实际发出去的请求体」和「SDK 实际解析回来的对象」，不是我们自己的想象。
SDK 2.x 建在 httpx2 上，传输层替身也得是 httpx2 的。
"""

import json
from typing import Any

import anthropic
import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from app.core.llm.anthropic import (
    NONSTREAM_MAX_TOKENS,
    STREAM_MAX_TOKENS,
    AnthropicProvider,
    LLMRefusalError,
)
from app.core.llm.base import (
    ImageBlock,
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
from app.core.llm.tool_stream import ToolCallAccumulator

MODEL = "claude-opus-5"
TOOL = {
    "name": "search_papers",
    "description": "Search the library",
    "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
}


def _message(content: list[dict[str, Any]], *, stop_reason: str = "end_turn", **extra: Any) -> dict:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 5},
        **extra,
    }


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _stream_events(
    blocks: list[dict[str, Any]], *, stop_reason: str = "end_turn", usage: dict | None = None
) -> list[dict[str, Any]]:
    """把一条完整消息拆成 Messages API 的流式事件序列。"""
    events: list[dict[str, Any]] = [
        {
            "type": "message_start",
            "message": _message([], stop_reason=None)
            | {"usage": usage or {"input_tokens": 12, "output_tokens": 0}},
        }
    ]
    for index, block in enumerate(blocks):
        kind = block["type"]
        if kind == "text":
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": part} for part in block["parts"]]
        elif kind == "thinking":
            start = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [{"type": "thinking_delta", "thinking": part} for part in block["parts"]]
            deltas.append({"type": "signature_delta", "signature": block["signature"]})
        else:
            start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": part} for part in block["parts"]]
        events.append({"type": "content_block_start", "index": index, "content_block": start})
        events.extend({"type": "content_block_delta", "index": index, "delta": d} for d in deltas)
        events.append({"type": "content_block_stop", "index": index})
    events.append(
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 7},
        }
    )
    events.append({"type": "message_stop"})
    return events


class Gateway:
    """记录请求、按脚本回响应的假服务端。"""

    def __init__(self, *responses: httpx2.Response) -> None:
        self._responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)

    def provider(
        self, *, max_attempts: int = 1, refusal_fallback: bool = False
    ) -> AnthropicProvider:
        client = AsyncAnthropic(
            api_key="sk-test",
            base_url="http://gw.test",
            max_retries=max_attempts - 1,
            http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self)),
        )
        return AnthropicProvider("sk-test", client=client, refusal_fallback=refusal_fallback)


def _json(payload: dict, status: int = 200, headers: dict | None = None) -> httpx2.Response:
    return httpx2.Response(status, json=payload, headers=headers)


def _stream(events: list[dict[str, Any]]) -> httpx2.Response:
    return httpx2.Response(200, content=_sse(events), headers={"content-type": "text/event-stream"})


def _error(status: int, kind: str, **headers: str) -> httpx2.Response:
    body = {"type": "error", "error": {"type": kind, "message": kind}}
    return httpx2.Response(status, json=body, headers=headers)


# ---- 请求构造 ----


async def test_request_goes_to_v1_messages_with_adaptive_thinking_and_no_sampling():
    gw = Gateway(_json(_message([{"type": "text", "text": "ok"}])))
    result = await gw.provider().complete(
        [Message("system", "You score papers."), Message("user", "hi")],
        model=MODEL,
        temperature=0.2,  # 调用方照旧传；当前模型不接受，不能发出去
        max_tokens=300,
        effort="low",
    )
    assert result.content == "ok" and result.finish_reason == "stop"
    assert str(gw.requests[0].url) == "http://gw.test/v1/messages"
    body = gw.body
    assert "temperature" not in body and "top_p" not in body and "top_k" not in body
    assert body["model"] == MODEL
    assert body["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in json.dumps(body)
    assert body["output_config"] == {"effort": "low"}
    # 思考和正文共用 max_tokens：调用方给的小数字要抬到下限，不然回答截在半路
    assert body["max_tokens"] == NONSTREAM_MAX_TOKENS
    assert body.get("stream") is not True
    # system 是顶层参数，第一段打缓存断点；messages 里不再有 system，也没有 assistant 预填
    assert body["system"] == [
        {"type": "text", "text": "You score papers.", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize(
    ("given", "sent"),
    [("none", "low"), ("minimal", "low"), ("low", "low"), ("medium", "medium"),
     ("high", "high"), ("xhigh", "xhigh"), ("max", "max")],
)  # fmt: skip
async def test_effort_levels_map_onto_claudes(given, sent):
    gw = Gateway(_json(_message([{"type": "text", "text": "ok"}])))
    await gw.provider().complete([Message("user", "hi")], model=MODEL, effort=given)
    assert gw.body["output_config"] == {"effort": sent}


async def test_no_effort_means_the_field_is_omitted():
    gw = Gateway(_json(_message([{"type": "text", "text": "ok"}])))
    await gw.provider().complete([Message("user", "hi")], model=MODEL)
    assert "output_config" not in gw.body


async def test_only_the_first_system_block_is_a_cache_breakpoint():
    gw = Gateway(_json(_message([{"type": "text", "text": "ok"}])))
    await gw.provider().complete(
        [
            Message("system", "stable"),
            Message("system", "changes every turn"),
            Message("user", "q"),
        ],
        model=MODEL,
    )
    system = gw.body["system"]
    assert [b["text"] for b in system] == ["stable", "changes every turn"]
    assert "cache_control" in system[0] and "cache_control" not in system[1]
    # 没有工具的一次性调用不开顶层自动缓存：尾巴每次都不同，开了只是白付写缓存的钱
    assert "cache_control" not in gw.body


async def test_tools_are_translated_and_turn_on_automatic_caching():
    gw = Gateway(_json(_message([{"type": "text", "text": "ok"}])))
    await gw.provider().complete(
        [Message("user", "q")], model=MODEL, tools=[TOOL], tool_choice="required"
    )
    body = gw.body
    assert body["tools"] == [
        {
            "name": "search_papers",
            "description": "Search the library",
            "input_schema": TOOL["parameters"],
        }
    ]
    assert body["tool_choice"] == {"type": "any"}
    assert body["cache_control"] == {"type": "ephemeral"}


async def test_forced_tool_choice_degrades_to_auto_where_the_model_rejects_it():
    gw = Gateway(_json(_message([{"type": "text", "text": "ok"}])))
    await gw.provider().complete(
        [Message("user", "q")], model="claude-opus-5-5", tools=[TOOL], tool_choice="required"
    )
    assert gw.body["tool_choice"] == {"type": "auto"}


async def test_images_ride_on_the_last_user_message():
    gw = Gateway(_json(_message([{"type": "text", "text": "ok"}])))
    await gw.provider().complete([Message("user", "look")], model=MODEL, images=[b"\x89PNG-bytes"])
    content = gw.body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["media_type"] == "image/png"
    assert content[1]["source"]["type"] == "base64"


async def test_refusal_fallback_is_sent_only_when_enabled():
    gw = Gateway(_json(_message([{"type": "text", "text": "ok"}])))
    await gw.provider(refusal_fallback=True).complete([Message("user", "q")], model=MODEL)
    assert gw.body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in gw.requests[0].headers["anthropic-beta"]

    off = Gateway(_json(_message([{"type": "text", "text": "ok"}])))
    await off.provider(refusal_fallback=False).complete([Message("user", "q")], model=MODEL)
    assert "fallbacks" not in off.body and "anthropic-beta" not in off.requests[0].headers


async def test_refusal_fallback_is_not_sent_for_models_without_it():
    gw = Gateway(_json(_message([{"type": "text", "text": "ok"}])))
    await gw.provider(refusal_fallback=True).complete(
        [Message("user", "q")], model="claude-haiku-4-5"
    )
    assert "fallbacks" not in gw.body


# ---- 读响应 ----


async def test_usage_counts_cached_input_as_input():
    """开了缓存后 input_tokens 只是没命中的那部分；记账要三项加起来。"""
    payload = _message([{"type": "text", "text": "ok"}]) | {
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 90,
            "output_tokens": 7,
        }
    }
    result = await Gateway(_json(payload)).provider().complete([Message("user", "q")], model=MODEL)
    assert result.usage["prompt_tokens"] == 1000
    assert result.usage["completion_tokens"] == 7
    assert result.usage["total_tokens"] == 1007
    assert result.usage["cache_read_input_tokens"] == 900


async def test_tool_use_round_trip_keeps_ids_and_thinking_signatures():
    first = _message(
        [
            {"type": "thinking", "thinking": "need to search", "signature": "sig-1"},
            {"type": "text", "text": "Let me look."},
            {"type": "tool_use", "id": "toolu_1", "name": "search_papers", "input": {"q": "rag"}},
        ],
        stop_reason="tool_use",
    )
    gw = Gateway(_json(first), _json(_message([{"type": "text", "text": "Found 2."}])))
    provider = gw.provider()
    history = [Message("user", "find rag papers")]
    result = await provider.complete(history, model=MODEL, tools=[TOOL])
    assert result.finish_reason == "tool_use"
    assert result.tool_calls == (ToolUseBlock("toolu_1", "search_papers", {"q": "rag"}),)
    assert result.blocks[0] == ThinkingBlock("need to search", "sig-1")

    history.append(result.as_assistant_message())
    history.append(
        Message(
            "user",
            [ToolResultBlock("toolu_1", '{"hits": 2}', images=(ImageBlock(b"img", "image/jpeg"),))],
        )
    )
    final = await provider.complete(history, model=MODEL, tools=[TOOL])
    assert final.content == "Found 2."

    replayed = gw.body["messages"]
    assert replayed[1] == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "need to search", "signature": "sig-1"},
            {"type": "text", "text": "Let me look."},
            {"type": "tool_use", "id": "toolu_1", "name": "search_papers", "input": {"q": "rag"}},
        ],
    }
    tool_result = replayed[2]["content"][0]
    assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_1"
    assert tool_result["content"][0] == {"type": "text", "text": '{"hits": 2}'}
    assert tool_result["content"][1]["source"]["media_type"] == "image/jpeg"


async def test_thinking_without_a_signature_is_not_replayed():
    """从落库的时间线重建的旧轮次没有签名；发过去是 400，而它本来就可以省略。"""
    gw = Gateway(_json(_message([{"type": "text", "text": "ok"}])))
    await gw.provider().complete(
        [
            Message("user", "q1"),
            Message("assistant", [ThinkingBlock("old thoughts"), TextBlock("a1")]),
            Message("user", "q2"),
        ],
        model=MODEL,
    )
    assert gw.body["messages"][1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "a1"}],
    }


async def test_a_refusal_raises_instead_of_returning_an_empty_answer():
    payload = _message([], stop_reason="refusal") | {
        "stop_details": {"type": "refusal", "category": "cyber", "explanation": "declined"}
    }
    with pytest.raises(LLMRefusalError) as exc:
        await Gateway(_json(payload)).provider().complete([Message("user", "q")], model=MODEL)
    assert exc.value.category == "cyber"
    assert "declined this request" in str(exc.value)


async def test_a_refusal_without_details_still_raises():
    payload = _message([], stop_reason="refusal")
    with pytest.raises(LLMRefusalError) as exc:
        await Gateway(_json(payload)).provider().complete([Message("user", "q")], model=MODEL)
    assert exc.value.category is None


async def test_hitting_max_tokens_is_reported_not_hidden():
    payload = _message([{"type": "text", "text": "half an ans"}], stop_reason="max_tokens")
    result = await Gateway(_json(payload)).provider().complete([Message("user", "q")], model=MODEL)
    assert result.finish_reason == "max_tokens" and result.content == "half an ans"


async def test_a_fallback_drops_the_declined_models_partial_work():
    """服务端换过模型：分界点之前的思考与工具调用作废，思考签名也不再回放。"""
    payload = _message(
        [
            {"type": "thinking", "thinking": "declined model", "signature": "sig-a"},
            {"type": "fallback", "from": {"model": MODEL}, "to": {"model": "claude-opus-4-8"}},
            {"type": "thinking", "thinking": "fallback model", "signature": "sig-b"},
            {"type": "text", "text": "answer"},
        ]
    ) | {"model": "claude-opus-4-8"}
    result = (
        await Gateway(_json(payload))
        .provider(refusal_fallback=True)
        .complete([Message("user", "q")], model=MODEL)
    )
    assert result.content == "answer" and result.model == "claude-opus-4-8"
    assert result.blocks == (ThinkingBlock("fallback model", None), TextBlock("answer"))


# ---- 流式 ----


async def test_long_outputs_switch_complete_to_streaming():
    gw = Gateway(_stream(_stream_events([{"type": "text", "parts": ["long ", "answer"]}])))
    result = await gw.provider().complete(
        [Message("user", "write a chapter")], model=MODEL, max_tokens=STREAM_MAX_TOKENS
    )
    assert gw.body["stream"] is True and gw.body["max_tokens"] == STREAM_MAX_TOKENS
    assert result.content == "long answer"
    assert result.usage["completion_tokens"] == 7


async def test_stream_events_yield_text_thinking_and_tool_calls_in_order():
    gw = Gateway(
        _stream(
            _stream_events(
                [
                    {"type": "thinking", "parts": ["plan ", "the search"], "signature": "sig-1"},
                    {"type": "text", "parts": ["Looking ", "it up."]},
                    {
                        "type": "tool_use",
                        "id": "toolu_9",
                        "name": "search_papers",
                        "parts": ['{"q": ', '"rag"}'],
                    },
                ],
                stop_reason="tool_use",
            )
        )
    )
    events = [
        ev
        async for ev in gw.provider().stream_events(
            [Message("user", "find rag")], model=MODEL, tools=[TOOL], effort="medium"
        )
    ]
    body = gw.body
    assert body["stream"] is True
    assert body["max_tokens"] == STREAM_MAX_TOKENS
    # 给人看的思考过程要显式要摘要，否则默认是空文本
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert "temperature" not in body

    assert events[:2] == [ThinkingDelta("plan "), ThinkingDelta("the search")]
    assert events[2] == ThinkingDelta("", "sig-1")
    assert events[3:5] == [TextDelta("Looking "), TextDelta("it up.")]
    assert events[5] == ToolUseStart(2, "toolu_9", "search_papers")
    assert events[6:8] == [ToolUseArgsDelta(2, '{"q": '), ToolUseArgsDelta(2, '"rag"}')]
    assert events[8] == ToolUseStop(2)
    done = events[-1]
    assert isinstance(done, StreamDone) and done.finish_reason == "tool_use"
    assert done.usage["prompt_tokens"] == 12 and done.usage["completion_tokens"] == 7

    # 攒成块之后能原样回放：签名跟着它自己的思考块，顺序不变
    acc = ToolCallAccumulator()
    for ev in events:
        acc.feed(ev)
    assert acc.finish() == (
        ThinkingBlock("plan the search", "sig-1"),
        TextBlock("Looking it up."),
        ToolUseBlock("toolu_9", "search_papers", {"q": "rag"}),
    )


async def test_each_thinking_block_keeps_its_own_signature():
    gw = Gateway(
        _stream(
            _stream_events(
                [
                    {"type": "thinking", "parts": ["first"], "signature": "sig-1"},
                    {"type": "text", "parts": ["note"]},
                    {"type": "thinking", "parts": ["second"], "signature": "sig-2"},
                    {"type": "tool_use", "id": "t1", "name": "search_papers", "parts": ["{}"]},
                ],
                stop_reason="tool_use",
            )
        )
    )
    acc = ToolCallAccumulator()
    async for ev in gw.provider().stream_events([Message("user", "q")], model=MODEL, tools=[TOOL]):
        acc.feed(ev)
    assert acc.finish() == (
        ThinkingBlock("first", "sig-1"),
        TextBlock("note"),
        ThinkingBlock("second", "sig-2"),
        ToolUseBlock("t1", "search_papers", {}),
    )


async def test_plain_stream_yields_only_text():
    gw = Gateway(
        _stream(
            _stream_events(
                [
                    {"type": "thinking", "parts": ["hmm"], "signature": "s"},
                    {"type": "text", "parts": ["a", "b"]},
                ]
            )
        )
    )
    chunks = [c async for c in gw.provider().stream([Message("user", "q")], model=MODEL)]
    assert chunks == ["a", "b"]


async def test_a_refusal_at_the_end_of_a_stream_raises():
    gw = Gateway(
        _stream(_stream_events([{"type": "text", "parts": ["part"]}], stop_reason="refusal"))
    )
    with pytest.raises(LLMRefusalError):
        async for _ in gw.provider().stream_events([Message("user", "q")], model=MODEL):
            pass


# ---- 错误与重试（SDK 自己做，这里只确认我们把次数传对了） ----


async def test_overload_is_retried_by_the_sdk():
    gw = Gateway(
        _error(529, "overloaded_error", **{"retry-after-ms": "1"}),
        _json(_message([{"type": "text", "text": "ok"}])),
    )
    result = await gw.provider(max_attempts=2).complete([Message("user", "q")], model=MODEL)
    assert result.content == "ok" and len(gw.requests) == 2


async def test_retries_are_bounded_by_max_attempts():
    gw = Gateway(_error(529, "overloaded_error", **{"retry-after-ms": "1"}))
    with pytest.raises(anthropic.APIStatusError):
        await gw.provider(max_attempts=2).complete([Message("user", "q")], model=MODEL)
    assert len(gw.requests) == 2


async def test_client_errors_surface_as_typed_exceptions_without_retry():
    gw = Gateway(_error(401, "authentication_error"))
    with pytest.raises(anthropic.AuthenticationError):
        await gw.provider(max_attempts=3).complete([Message("user", "q")], model=MODEL)
    assert len(gw.requests) == 1

    bad = Gateway(_error(400, "invalid_request_error"))
    with pytest.raises(anthropic.BadRequestError):
        await bad.provider(max_attempts=3).complete([Message("user", "q")], model=MODEL)
    assert len(bad.requests) == 1


async def test_ping_is_a_tiny_request():
    gw = Gateway(_json(_message([{"type": "text", "text": "OK"}])))
    await gw.provider(refusal_fallback=True).ping(MODEL)
    body = gw.body
    assert body["max_tokens"] <= 32 and body["thinking"] == {"type": "disabled"}
    assert "fallbacks" not in body
