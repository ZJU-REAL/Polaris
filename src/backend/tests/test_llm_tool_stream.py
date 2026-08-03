"""流式工具调用的解析：累加器 + 两家 provider 的 chunk 形状。

夹具全部手写，不联网。这里覆盖的每一条都是真实中转上见过的差异——它们在正常流量里
不显形，出问题时又极难复现，所以只能靠这组测试天天跑。
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
    normalize_finish_reason,
)
from app.core.llm.tool_stream import ToolCallAccumulator

# ---- 累加器 ----


def test_arguments_are_reassembled_from_arbitrary_fragments():
    """参数按 index 拼回来，切口在哪都行。

    OpenAI 的 arguments 是逐段的 JSON 字符串，切口可能落在键名中间、值中间、甚至
    转义序列中间。所以必须**按原始字符串累加、到最后才 json.loads**，中途任何一次
    解析都会失败。
    """
    acc = ToolCallAccumulator()
    raw = json.dumps({"query": '带"引号"和\\反斜杠', "k": 5}, ensure_ascii=False)
    acc.feed(ToolUseStart(0, "call_1", "search_papers"))
    for i in range(0, len(raw), 3):  # 3 个字符一刀，切得足够碎
        acc.feed(ToolUseArgsDelta(0, raw[i : i + 3]))
    acc.feed(ToolUseStop(0))

    blocks = acc.finish()
    assert len(blocks) == 1
    call = blocks[0]
    assert isinstance(call, ToolUseBlock)
    assert call.name == "search_papers"
    assert call.input == {"query": '带"引号"和\\反斜杠', "k": 5}


def test_parallel_calls_are_merged_by_index_not_by_id():
    """两个并行调用按 index 归并。

    id 不能当归并键：有的中转只在首片发 id，有的每片都发，还有的压根不发。
    """
    acc = ToolCallAccumulator()
    acc.feed(ToolUseStart(0, "a", "search_papers"))
    acc.feed(ToolUseStart(1, "b", "get_paper"))
    acc.feed(ToolUseArgsDelta(0, '{"query"'))
    acc.feed(ToolUseArgsDelta(1, '{"paper_id"'))
    acc.feed(ToolUseArgsDelta(0, ': "x"}'))
    acc.feed(ToolUseArgsDelta(1, ': "y"}'))

    calls = [b for b in acc.finish() if isinstance(b, ToolUseBlock)]
    assert [c.name for c in calls] == ["search_papers", "get_paper"]
    assert calls[0].input == {"query": "x"} and calls[1].input == {"paper_id": "y"}


def test_id_locks_on_first_non_empty_value():
    """id/name 首次非空即锁定：后续分片重发不覆盖，也不因空值把已有的抹掉。"""
    acc = ToolCallAccumulator()
    acc.feed(ToolUseStart(0, "real-id", "search_papers"))
    acc.feed(ToolUseStart(0, "", ""))  # 有的中转会重发一个空壳
    acc.feed(ToolUseArgsDelta(0, "{}"))
    call = acc.finish()[0]
    assert isinstance(call, ToolUseBlock)
    assert call.id == "real-id" and call.name == "search_papers"


def test_empty_arguments_become_an_empty_dict():
    """空参工具流出的是 ``""``（或一片都不发），这不是错误。"""
    acc = ToolCallAccumulator()
    acc.feed(ToolUseStart(0, "c", "get_project_status"))
    acc.feed(ToolUseArgsDelta(0, ""))
    assert acc.finish()[0].input == {}

    acc2 = ToolCallAccumulator()
    acc2.feed(ToolUseStart(0, "c", "get_project_status"))
    assert acc2.finish()[0].input == {}


def test_malformed_arguments_do_not_raise():
    """参数不是合法 JSON 时不抛——抛出去会把整条流打断。

    弱模型上这是高频情形，模型下一轮完全可以自己纠正，所以留一个标记让调用方去告诉它。
    """
    acc = ToolCallAccumulator()
    acc.feed(ToolUseStart(0, "c", "search_papers"))
    acc.feed(ToolUseArgsDelta(0, '{"query": '))  # 截断的 JSON
    call = acc.finish()[0]
    assert "__parse_error__" in call.input


def test_text_and_thinking_are_kept_with_the_signature():
    """thinking 的签名必须留在块里：回放时缺签名会被 Anthropic 拒掉。"""
    acc = ToolCallAccumulator()
    acc.feed(ThinkingDelta("先想想"))
    acc.feed(ThinkingDelta("", "sig-abc"))
    acc.feed(TextDelta("答案是"))
    blocks = acc.finish()
    assert isinstance(blocks[0], ThinkingBlock)
    assert blocks[0].text == "先想想" and blocks[0].signature == "sig-abc"
    assert isinstance(blocks[1], TextBlock) and blocks[1].text == "答案是"


def test_finish_reason_is_normalised():
    assert normalize_finish_reason("tool_calls") == "tool_use"
    assert normalize_finish_reason("tool_use") == "tool_use"
    assert normalize_finish_reason("end_turn") == "stop"
    assert normalize_finish_reason("length") == "max_tokens"
    assert normalize_finish_reason(None) is None


# ---- OpenAI 兼容 provider ----


def _sse(chunks: list[dict]) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _openai_provider(chunks: list[dict]):
    from app.core.llm.openai_compat import OpenAICompatProvider

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(chunks))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatProvider("http://x/v1", "k", client=client)


async def _events(provider, **kwargs):
    return [
        ev
        async for ev in provider.stream_events(
            [Message(role="user", content="hi")], model="m", **kwargs
        )
    ]


@pytest.mark.asyncio
async def test_openai_text_and_tool_call_in_the_same_chunk():
    """``delta.content`` 与 ``delta.tool_calls`` 可能出现在**同一个 chunk** 里。

    写成 if/elif 就会丢掉其中一个——这是最容易犯、也最难从现象发现的错。
    """
    provider = _openai_provider(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "content": "我来查一下",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "search_papers", "arguments": "{}"},
                                }
                            ],
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    events = await _events(provider)
    assert any(isinstance(e, TextDelta) and e.text == "我来查一下" for e in events)
    assert any(isinstance(e, ToolUseStart) and e.name == "search_papers" for e in events)
    done = events[-1]
    assert isinstance(done, StreamDone) and done.finish_reason == "tool_use"


@pytest.mark.asyncio
async def test_openai_tool_call_without_index_defaults_to_zero():
    """单个工具调用时部分中转不发 index。缺了就当 0，不能抛。"""
    provider = _openai_provider(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"id": "c1", "function": {"name": "get_paper", "arguments": '{"a"'}}
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": ': 1}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
    )
    acc = ToolCallAccumulator()
    for ev in await _events(provider):
        acc.feed(ev)
    call = [b for b in acc.finish() if isinstance(b, ToolUseBlock)][0]
    assert call.name == "get_paper" and call.input == {"a": 1}


@pytest.mark.asyncio
async def test_openai_stream_is_a_filter_over_stream_events():
    """``stream()`` 只吐文本，且与 ``stream_events`` 共用同一份 SSE 解析。"""
    provider = _openai_provider(
        [
            {"choices": [{"delta": {"content": "一"}}]},
            {"choices": [{"delta": {"reasoning_content": "（思考）"}}]},
            {"choices": [{"delta": {"content": "二"}, "finish_reason": "stop"}]},
        ]
    )
    chunks = [
        c async for c in provider.stream([Message(role="user", content="hi")], model="m")
    ]
    assert chunks == ["一", "二"], "思考不该混进正文"

    events = await _events(provider)
    assert any(isinstance(e, ThinkingDelta) for e in events), "但结构化流里要能拿到思考"


@pytest.mark.asyncio
async def test_openai_payload_carries_tools_and_tool_results_fan_out():
    """工具定义按 OpenAI 形状发；一条含 K 个结果的 user 消息扇出成 K 条 role=tool。"""
    from app.core.llm.openai_compat import OpenAICompatProvider

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=_sse([{"choices": [{"delta": {"content": "ok"}}]}]))

    provider = OpenAICompatProvider(
        "http://x/v1", "k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    messages = [
        Message(role="user", content="查一下"),
        Message(
            role="assistant",
            content=[ToolUseBlock("c1", "search_papers", {"query": "x"})],
        ),
        Message(
            role="user",
            content=[
                ToolResultBlock("c1", '{"hits": 3}'),
                ToolResultBlock("c2", '{"hits": 0}'),
            ],
        ),
    ]
    async for _ in provider.stream_events(
        messages,
        model="m",
        tools=[{"name": "search_papers", "description": "d", "parameters": {"type": "object"}}],
        tool_choice="auto",
    ):
        pass

    assert seen["tools"][0]["function"]["name"] == "search_papers"
    assert seen["tool_choice"] == "auto"
    roles = [m["role"] for m in seen["messages"]]
    assert roles.count("tool") == 2, f"两个结果要扇出成两条 role=tool：{roles}"
    assert seen["messages"][1]["tool_calls"][0]["id"] == "c1"


# ---- Anthropic provider ----


def _anthropic_provider(events: list[dict]):
    from app.core.llm.anthropic import AnthropicProvider

    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body)

    return AnthropicProvider("k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_anthropic_tool_use_blocks_and_usage():
    """content_block_start/input_json_delta/stop 的三段式，外加两处 usage。

    usage 此前完全没解析（代码里就写着 TODO），而且键名是 input_tokens/output_tokens，
    与记账口径不同——不归一化的话走 Anthropic 的用量全是 len/4 估算值。
    """
    provider = _anthropic_provider(
        [
            {"type": "message_start", "message": {"usage": {"input_tokens": 120}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "t1", "name": "read_fulltext"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": '{"paper_'},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": 'id": "p1"}'},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 45},
            },
        ]
    )
    acc = ToolCallAccumulator()
    events = []
    async for ev in provider.stream_events([Message(role="user", content="hi")], model="m"):
        events.append(ev)
        acc.feed(ev)

    call = [b for b in acc.finish() if isinstance(b, ToolUseBlock)][0]
    assert call.name == "read_fulltext" and call.input == {"paper_id": "p1"}

    done = events[-1]
    assert isinstance(done, StreamDone)
    assert done.finish_reason == "tool_use"
    assert done.usage["prompt_tokens"] == 120 and done.usage["completion_tokens"] == 45


@pytest.mark.asyncio
async def test_anthropic_thinking_signature_round_trips():
    """签名要能完整往返，否则带 thinking 的历史回放会被 400。"""
    provider = _anthropic_provider(
        [
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "想一下"},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig-1"},
            },
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        ]
    )
    acc = ToolCallAccumulator()
    async for ev in provider.stream_events([Message(role="user", content="hi")], model="m"):
        acc.feed(ev)
    block = acc.finish()[0]
    assert isinstance(block, ThinkingBlock) and block.signature == "sig-1"


@pytest.mark.asyncio
async def test_anthropic_tool_results_carry_images_natively():
    """Anthropic 的 tool_result 能原生带图；OpenAI 侧做不到，只能另发一条合成消息。"""
    from app.core.llm.base import ImageBlock

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, content=b'data: {"type": "message_delta", "delta": {}}\n\n')

    from app.core.llm.anthropic import AnthropicProvider

    provider = AnthropicProvider(
        "k", client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    messages = [
        Message(
            role="user",
            content=[ToolResultBlock("t1", '{"ok": 1}', images=(ImageBlock(b"\x89PNG"),))],
        )
    ]
    async for _ in provider.stream_events(messages, model="m"):
        pass

    content = seen["messages"][0]["content"][0]
    assert content["type"] == "tool_result" and content["tool_use_id"] == "t1"
    kinds = [part["type"] for part in content["content"]]
    assert kinds == ["text", "image"]
