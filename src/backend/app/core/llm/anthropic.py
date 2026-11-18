"""Claude provider，基于官方 ``anthropic`` SDK（``AsyncAnthropic``）。

Polaris 的所有对话类调用都走这里（见 router.py）。几条针对当前 Claude 模型的约定：

- **不发采样参数**。``temperature`` / ``top_p`` / ``top_k`` 在 Opus 5 一代上是 400，
  接口上保留 ``temperature`` 形参只为兼容 ``LLMProvider`` 的签名，值一律忽略。
- **思考默认开**（自适应）。深度用 ``output_config.effort`` 控制，不发 ``budget_tokens``。
  ``max_tokens`` 是「思考 + 正文」合在一起的硬上限，所以这里不接受很小的值：调用方
  传的小数字是给不思考的模型定的，照发会把回答截在半路。
- **长输出走流式**。``max_tokens`` 超过 :data:`NONSTREAM_MAX_TOKENS` 时 ``complete``
  内部改用 ``.stream()`` + ``get_final_message()``，SDK 对大 ``max_tokens`` 的非流式
  请求会直接拒绝（怕连接空等超时）。
- **重试交给 SDK**：429 / 5xx / 连接错误它自己退避重试，次数由 ``max_attempts`` 定。
- **拒答不是空回答**：``stop_reason == "refusal"`` 抛 :class:`LLMRefusalError`，
  调用方不会拿到一段空字符串再去解析。
- **提示缓存**：第一段 system 打缓存断点（各环节的系统提示是稳定前缀）；带工具的
  多轮循环再加顶层自动缓存，让越来越长的对话前缀也能命中。
"""

import base64
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel

from app.core.llm.base import (
    CompletionResult,
    ContentBlock,
    EffortLevel,
    ImageBlock,
    LLMProvider,
    Message,
    StreamDone,
    StreamEvent,
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
    structured_stream_started,
)
from app.core.llm.concurrency import limit_for
from app.core.llm.errors import LLMRefusalError, LLMTransportError

logger = logging.getLogger("polaris.llm")

#: 非流式请求的 ``max_tokens`` 上限，也是默认值。再大就必须流式。
NONSTREAM_MAX_TOKENS = 16_000
#: 流式请求的默认 ``max_tokens``：超时不再是问题，给模型留足思考和输出的余量。
STREAM_MAX_TOKENS = 64_000
#: 当前模型的输出上限。
_MAX_OUTPUT_TOKENS = 128_000

#: 平台的档位 → Claude 的 effort。Claude 没有 none / minimal，按最低档发。
_EFFORT_MAP: dict[str, str] = {
    "none": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}

#: 拒答时让服务端换模型重跑（server-side fallbacks，beta）。只对支持它的模型发。
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = frozenset(
    {
        "claude-opus-5",
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-fable-5-1",
        "claude-fable-5-2",
    }
)
#: 这些模型不接受强制调用工具（tool_choice any / tool 是 400），退回 auto。
_NO_FORCED_TOOL_CHOICE = frozenset(
    {
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-fable-5-1",
        "claude-fable-5-2",
        "claude-mythos-5-1",
        "claude-mythos-5-2",
    }
)


def bare_model(model: str) -> str:
    """去掉网关风格的 ``anthropic/`` 前缀，得到官方模型名。"""
    return model.rsplit("/", 1)[-1]


def _image_payload(data: bytes, mime: str) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": mime,
            "data": base64.b64encode(data).decode("ascii"),
        },
    }


def _content_payload(block: ContentBlock) -> dict[str, Any] | None:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text} if block.text else None
    if isinstance(block, ThinkingBlock):
        # 思考块只有带着原签名才能回放；没有签名的（从落库的时间线重建的旧轮次）
        # 发过去是 400，而旧轮次的思考本来就可以省略
        if not block.signature:
            return None
        return {"type": "thinking", "thinking": block.text, "signature": block.signature}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ImageBlock):
        return _image_payload(block.data, block.mime)
    if not isinstance(block, ToolResultBlock):  # 防御：将来加了新块类型别静默塞错形状
        raise TypeError(f"unsupported content block: {type(block).__name__}")
    # 图片作为工具结果内容的一部分，这是 Messages API 原生支持的形状
    content: list[dict[str, Any]] = [{"type": "text", "text": block.content}]
    content.extend(_image_payload(img.data, img.mime) for img in block.images)
    out: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": block.tool_use_id,
        "content": content,
    }
    if block.is_error:
        out["is_error"] = True
    return out


def _usage_dict(usage: Any) -> dict[str, int]:
    """SDK 的 usage → 平台记账口径。

    开了提示缓存之后 ``input_tokens`` 只是**没命中缓存**的那部分，读缓存和写缓存各有
    自己的字段；记账要的是这次请求一共送进去多少，所以三项加起来才是 prompt_tokens。
    """
    if usage is None:
        return {}

    def field(name: str) -> int:
        value = getattr(usage, name, None)
        return int(value) if isinstance(value, int) else 0

    cache_read = field("cache_read_input_tokens")
    cache_write = field("cache_creation_input_tokens")
    prompt = field("input_tokens") + cache_read + cache_write
    completion = field("output_tokens")
    out = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if cache_read:
        out["cache_read_input_tokens"] = cache_read
    if cache_write:
        out["cache_creation_input_tokens"] = cache_write
    return out


def _blocks_from_message(message: Any) -> tuple[list[ContentBlock], str]:
    """SDK 的 Message → 平台的块列表 + 拼好的正文。

    服务端换过模型（内容里出现 ``fallback`` 块）时，分界点之前的思考与工具调用是
    被拒模型留下的半截产物，不能回放；整条消息的思考签名也不再对得上发起请求的
    模型，所以一并去掉签名（没有签名的思考块不会被回放，见 ``_content_payload``）。
    """
    raw = list(getattr(message, "content", None) or [])
    last_fallback = max((i for i, b in enumerate(raw) if b.type == "fallback"), default=-1)
    blocks: list[ContentBlock] = []
    texts: list[str] = []
    for index, block in enumerate(raw):
        kind = block.type
        if kind == "text":
            texts.append(block.text)
            blocks.append(TextBlock(block.text))
        elif kind == "thinking":
            if index < last_fallback:
                continue
            signature = None if last_fallback >= 0 else block.signature
            blocks.append(ThinkingBlock(block.thinking or "", signature))
        elif kind == "tool_use":
            if index < last_fallback:
                continue
            blocks.append(ToolUseBlock(block.id, block.name, dict(block.input or {})))
    return blocks, "".join(texts)


async def _final_message(stream: Any) -> Any:
    """流读完后取完整消息。流被网关中途掐断时 SDK 在这里是一个光秃秃的
    ``AssertionError``（没有收到收尾事件）——那是连接问题，换成带 ``transient`` 的错误，
    上层才会按「过一会儿再试」处理，而不是当成程序内部出错。"""
    try:
        return await stream.get_final_message()
    except AssertionError as e:
        raise LLMTransportError("model stream ended before the final message") from e


def _is_overload(error: BaseException) -> bool:
    """接口在说「忙」：限流、过载、没有可用账号，或者流被中途掐断、连不上。"""
    if isinstance(error, anthropic.RateLimitError | LLMTransportError):
        return True
    if isinstance(error, anthropic.APIStatusError):
        return error.status_code in (503, 529)
    return isinstance(error, anthropic.APIConnectionError)


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    supports_tools = True

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float = 300.0,
        max_attempts: int = 3,
        refusal_fallback: bool = True,
        client: AsyncAnthropic | None = None,
    ) -> None:
        self._refusal_fallback = refusal_fallback
        # max_attempts 是「一共试几次」，SDK 的 max_retries 是「失败后再试几次」
        self._client = client or AsyncAnthropic(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout,
            max_retries=max(0, max_attempts - 1),
        )
        # 同一个接口地址共用一道并发闸（core/llm/concurrency.py）
        self._limit_key = f"anthropic:{base_url or ''}"

    @contextlib.asynccontextmanager
    async def _gated(self) -> AsyncIterator[None]:
        """占一个在途名额跑一次请求；接口说忙就让闸收紧，成功了记一次。"""
        gate = limit_for(self._limit_key)
        async with gate.slot():
            try:
                yield
            except BaseException as e:
                if _is_overload(e):
                    gate.overloaded()
                raise
        await gate.succeeded()

    # ---- 请求构造 ----

    def _request(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        max_tokens: int,
        images: list[bytes] | None,
        effort: EffortLevel | None,
        tools: Sequence[dict[str, Any]] | None,
        tool_choice: str | None,
        show_thinking: bool,
        output_schema: type[BaseModel] | None = None,
        cache_prefix: str | None = None,
    ) -> dict[str, Any]:
        """拼 ``messages.create`` / ``messages.stream`` 的关键字参数。"""
        # system 提示是顶层参数，不在 messages 里。每条单独成块，顺序不变
        system_blocks: list[dict[str, Any]] = [
            {"type": "text", "text": m.text} for m in messages if m.role == "system" and m.text
        ]
        payload_messages: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            if isinstance(m.content, str):
                payload_messages.append({"role": m.role, "content": m.content})
                continue
            parts = [p for p in (_content_payload(b) for b in m.content) if p is not None]
            if parts:
                payload_messages.append({"role": m.role, "content": parts})

        if images and payload_messages:
            target = next(
                (m for m in reversed(payload_messages) if m["role"] == "user"),
                payload_messages[-1],
            )
            parts = (
                target["content"]
                if isinstance(target["content"], list)
                else [{"type": "text", "text": target["content"]}]
            )
            parts.extend(_image_payload(image, "image/png") for image in images)
            target["content"] = parts

        if cache_prefix:
            # 共享前缀放在第一条 user 消息最前面并打缓存断点：同一前缀、不同提问的
            # 多次调用只有第一次按原价读这段材料。断点之后才是每次都变的内容。
            prefix_block = {
                "type": "text",
                "text": cache_prefix,
                "cache_control": {"type": "ephemeral"},
            }
            first_user = next((m for m in payload_messages if m["role"] == "user"), None)
            if first_user is None:
                payload_messages.insert(0, {"role": "user", "content": [prefix_block]})
            else:
                rest = (
                    first_user["content"]
                    if isinstance(first_user["content"], list)
                    else [{"type": "text", "text": first_user["content"]}]
                )
                first_user["content"] = [prefix_block, *rest]

        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": payload_messages,
            # 自适应思考。要给人看思考过程的（流式事件）显式要摘要，否则默认是空文本
            "thinking": (
                {"type": "adaptive", "display": "summarized"}
                if show_thinking
                else {"type": "adaptive"}
            ),
        }
        if system_blocks:
            # 断点打在第一段：它是各环节固定的系统提示，后面几段（记忆、当前页面等）
            # 每轮都可能变。工具定义排在 system 之前，这个断点把它们一起缓存了。
            # 前缀不够长（Opus 5 是 512 token）时服务端不缓存，也不报错。
            system_blocks[0]["cache_control"] = {"type": "ephemeral"}
            request["system"] = system_blocks
        if effort is not None:
            request["output_config"] = {"effort": _EFFORT_MAP.get(effort, "high")}
        if output_schema is not None:
            # 结构化输出：服务端按 schema 约束解码，回来的第一段文本一定是合法 JSON。
            # transform_schema 去掉接口不支持的约束（长度、数值范围等）并补上
            # additionalProperties: false；被去掉的约束由调用方拿 Pydantic 再校验一遍
            request.setdefault("output_config", {})["format"] = {
                "type": "json_schema",
                "schema": anthropic.transform_schema(output_schema),
            }
        if tools:
            request["tools"] = [
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "input_schema": t.get("parameters") or {"type": "object", "properties": {}},
                }
                for t in tools
            ]
            # 多轮工具循环：对话前缀每轮都在变长，顶层自动缓存让上一轮的前缀可复用
            request["cache_control"] = {"type": "ephemeral"}
            if tool_choice == "none":
                request["tool_choice"] = {"type": "none"}
            elif tool_choice == "required" and bare_model(model) not in _NO_FORCED_TOOL_CHOICE:
                request["tool_choice"] = {"type": "any"}
            elif tool_choice in ("auto", "required"):
                request["tool_choice"] = {"type": "auto"}
        return request

    def _api(self, model: str) -> tuple[Any, dict[str, Any]]:
        """(messages 资源, 额外参数)。开了拒答回退且模型支持时走 beta 接口。"""
        if self._refusal_fallback and bare_model(model) in _FALLBACK_MODELS:
            return self._client.beta.messages, {"betas": [_FALLBACK_BETA], "fallbacks": "default"}
        return self._client.messages, {}

    @staticmethod
    def _result(message: Any, model: str) -> CompletionResult:
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMRefusalError(
                getattr(details, "category", None), getattr(details, "explanation", None)
            )
        if message.stop_reason == "max_tokens":
            logger.warning("claude %s: response hit max_tokens and was cut off", model)
        blocks, text = _blocks_from_message(message)
        return CompletionResult(
            content=text,
            model=message.model or model,
            finish_reason=normalize_finish_reason(message.stop_reason),
            usage=_usage_dict(message.usage),
            blocks=tuple(blocks),
        )

    # ---- LLMProvider 接口 ----

    async def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float | None = None,  # noqa: ARG002 — 当前模型不接受采样参数
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> CompletionResult:
        limit = min(max(max_tokens or 0, NONSTREAM_MAX_TOKENS), _MAX_OUTPUT_TOKENS)
        request = self._request(
            messages,
            model=model,
            max_tokens=limit,
            images=images,
            effort=effort,
            tools=tools,
            tool_choice=tool_choice,
            show_thinking=False,
        )
        api, extra = self._api(model)
        async with self._gated():
            if limit > NONSTREAM_MAX_TOKENS:
                # 长输出：流式收齐再返回，连接上一直有数据，不会被空等超时掐断
                async with api.stream(**request, **extra) as stream:
                    message = await _final_message(stream)
            else:
                message = await api.create(**request, **extra)
        return self._result(message, model)

    async def complete_structured(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        schema: type[BaseModel],
        max_tokens: int | None = None,
        effort: EffortLevel | None = None,
        cache_prefix: str | None = None,
    ) -> CompletionResult:
        """结构化输出（``output_config.format``）：返回 JSON 原文与结束原因。

        与 ``complete`` 不同，这里**照发**调用方给的 ``max_tokens``，不抬到下限：
        路由器（``complete_json``）自己定默认值，并在被截断时放宽重问。拒答抛
        :class:`LLMRefusalError`；被截断（``finish_reason == "max_tokens"``）时照常
        返回，由路由器决定怎么办——那段 JSON 是半截的，不能解析。
        """
        limit = min(max(max_tokens or NONSTREAM_MAX_TOKENS, 1), _MAX_OUTPUT_TOKENS)
        request = self._request(
            messages,
            model=model,
            max_tokens=limit,
            images=None,
            effort=effort,
            tools=None,
            tool_choice=None,
            show_thinking=False,
            output_schema=schema,
            cache_prefix=cache_prefix,
        )
        api, extra = self._api(model)
        started = structured_stream_started.get()
        async with self._gated():
            if limit > NONSTREAM_MAX_TOKENS or started is not None:
                async with api.stream(**request, **extra) as stream:
                    if started is not None:
                        # 第一个事件到了 = 服务端已读完提示、缓存已写好：告诉等着扇出的
                        # 调用方。迭代器要留着读完再取整条消息（半途丢掉的异步生成器被
                        # 回收时会关流）
                        events = stream.__aiter__()
                        with contextlib.suppress(StopAsyncIteration):
                            await events.__anext__()
                        started.set()
                        async for _event in events:
                            pass
                    message = await _final_message(stream)
            else:
                message = await api.create(**request, **extra)
        return self._result(message, model)

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
    ) -> AsyncIterator[str]:
        """纯文本流式：``stream_events`` 的过滤器（只有一份事件解析）。"""
        async for ev in self.stream_events(
            messages,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            images=images,
            effort=effort,
        ):
            if isinstance(ev, TextDelta):
                yield ev.text

    async def stream_events(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float | None = None,  # noqa: ARG002 — 当前模型不接受采样参数
        max_tokens: int | None = None,
        images: list[bytes] | None = None,
        effort: EffortLevel | None = None,
        tools: Sequence[dict[str, Any]] | None = None,
        tool_choice: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """结构化流式：文本、思考（含签名）、工具调用的开始 / 参数分片 / 结束。

        签名随 ``ThinkingDelta("", signature)`` 给出，一个思考块一个；下一轮回放这条
        assistant 消息时必须原样带回（见 tool_stream.ToolCallAccumulator）。
        """
        limit = min(max(max_tokens or 0, STREAM_MAX_TOKENS), _MAX_OUTPUT_TOKENS)
        request = self._request(
            messages,
            model=model,
            max_tokens=limit,
            images=images,
            effort=effort,
            tools=tools,
            tool_choice=tool_choice,
            show_thinking=True,
        )
        api, extra = self._api(model)
        tool_indexes: set[int] = set()
        async with self._gated(), api.stream(**request, **extra) as stream:
            async for event in stream:
                kind = event.type
                if kind == "content_block_start":
                    block = event.content_block
                    if block.type == "tool_use":
                        tool_indexes.add(event.index)
                        yield ToolUseStart(event.index, block.id, block.name)
                elif kind == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        if delta.text:
                            yield TextDelta(delta.text)
                    elif delta.type == "input_json_delta":
                        yield ToolUseArgsDelta(event.index, delta.partial_json or "")
                    elif delta.type == "thinking_delta":
                        if delta.thinking:
                            yield ThinkingDelta(delta.thinking)
                    elif delta.type == "signature_delta":
                        yield ThinkingDelta("", delta.signature)
                elif kind == "content_block_stop" and event.index in tool_indexes:
                    yield ToolUseStop(event.index)
            message = await _final_message(stream)
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            raise LLMRefusalError(
                getattr(details, "category", None), getattr(details, "explanation", None)
            )
        if message.stop_reason == "max_tokens":
            logger.warning("claude %s: stream hit max_tokens and was cut off", model)
        yield StreamDone(
            finish_reason=normalize_finish_reason(message.stop_reason),
            usage=_usage_dict(message.usage),
        )

    async def ping(self, model: str) -> None:
        """最小的一次真实调用，只为确认 key、地址、模型三样都对。

        失败时原样抛 SDK 的异常，由调用方（状态检查接口）翻译成人话。
        """
        await self._client.with_options(max_retries=0, timeout=30.0).messages.create(
            model=model,
            max_tokens=16,
            # 关掉思考并压到最低档：这是连通性探测，不需要模型动脑
            thinking={"type": "disabled"},
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": "Reply with OK."}],
        )

    async def aclose(self) -> None:
        await self._client.close()


__all__ = [
    "NONSTREAM_MAX_TOKENS",
    "STREAM_MAX_TOKENS",
    "AnthropicProvider",
    "LLMRefusalError",
    "anthropic",
    "bare_model",
]
