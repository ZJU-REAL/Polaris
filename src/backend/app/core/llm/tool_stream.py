"""流式工具调用的累加器。

两家的协议看着不一样，本质是同一件事：**身份在 start 事件上给全，参数按 index 增量拼**。

- OpenAI：``delta.tool_calls[]``，每个分片带 ``index``；``id`` 与 ``function.name`` 通常
  只在该 index 的第一个分片出现；``function.arguments`` 是逐段的 JSON 字符串。
- Anthropic：``content_block_start``（index + id + name）→ ``content_block_delta``
  （``input_json_delta.partial_json``）→ ``content_block_stop``。

所以归并靠 ``index`` 而不是 ``id`` —— OpenAI 的 id 有的中转只发一次、有的每片都发，
拿它当归并键会散。

本模块不做 IO、不 import provider，可以直接单测。参数分片的解析路径只有单测能天天
跑到（真实流量里它是对的，出问题时又极难复现），所以 FakeProvider 会故意把 JSON 切在
键名中间、值中间、转义序列中间。
"""

import json
import logging
from typing import Any

from app.core.llm.base import (
    ContentBlock,
    StreamEvent,
    TextBlock,
    TextDelta,
    ThinkingBlock,
    ThinkingDelta,
    ToolUseArgsDelta,
    ToolUseBlock,
    ToolUseStart,
)

logger = logging.getLogger(__name__)


class _Pending:
    __slots__ = ("id", "name", "args")

    def __init__(self, id: str, name: str) -> None:
        self.id = id
        self.name = name
        self.args: list[str] = []


class ToolCallAccumulator:
    """把流式事件攒成完整的 content blocks。

    用法：每收到一个 :class:`StreamEvent` 调一次 :meth:`feed`，流结束后调 :meth:`finish`
    拿到块列表。文本与思考也一并攒着，因为 assistant 轮回喂时要的是**完整的块序列**
    （thinking 的签名、tool_use 的 id 都在里面）。
    """

    def __init__(self) -> None:
        self._text: list[str] = []
        self._thinking: list[str] = []
        self._thinking_signature: str | None = None
        self._pending: dict[int, _Pending] = {}
        self._order: list[int] = []

    def feed(self, ev: StreamEvent) -> None:
        if isinstance(ev, TextDelta):
            self._text.append(ev.text)
        elif isinstance(ev, ThinkingDelta):
            self._thinking.append(ev.text)
            if ev.signature:
                self._thinking_signature = ev.signature
        elif isinstance(ev, ToolUseStart):
            if ev.index not in self._pending:
                self._pending[ev.index] = _Pending(ev.id, ev.name)
                self._order.append(ev.index)
            else:
                # 有的中转在后续分片里重发 id/name：首次非空即锁定，不覆盖
                cur = self._pending[ev.index]
                cur.id = cur.id or ev.id
                cur.name = cur.name or ev.name
        elif isinstance(ev, ToolUseArgsDelta):
            cur = self._pending.get(ev.index)
            if cur is None:
                # 没见过 start 就先来了参数：补一个占位，name 留空，finish 时丢弃
                cur = _Pending("", "")
                self._pending[ev.index] = cur
                self._order.append(ev.index)
            cur.args.append(ev.json_fragment)
        # ToolUseStop / StreamDone 不需要动作：参数一律在 finish 时统一解析

    @property
    def text(self) -> str:
        return "".join(self._text)

    @property
    def has_calls(self) -> bool:
        return any(p.name for p in self._pending.values())

    def finish(self) -> tuple[ContentBlock, ...]:
        """产出块列表：thinking → text → 各个 tool_use（按 index 首次出现的顺序）。"""
        blocks: list[ContentBlock] = []
        thinking = "".join(self._thinking)
        if thinking:
            blocks.append(ThinkingBlock(thinking, self._thinking_signature))
        text = self.text
        if text:
            blocks.append(TextBlock(text))
        for index in self._order:
            pending = self._pending[index]
            if not pending.name:
                logger.warning("tool call at index %s has no name; dropped", index)
                continue
            call_id = pending.id or f"call_{index}"
            blocks.append(ToolUseBlock(call_id, pending.name, _parse_args(pending)))
        return tuple(blocks)


def _parse_args(pending: _Pending) -> dict[str, Any]:
    """把攒起来的 JSON 分片解析成参数。

    空参工具会流出 ``""``（有的中转干脆一个分片都不发），这不是错误，兜底成 ``{}``。
    真的解析不了时也**不抛**——抛出去会把整条流打断，而模型下一轮完全可以自己纠正。
    留一个 ``__parse_error__`` 让调用方决定怎么告诉模型。
    """
    raw = "".join(pending.args).strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        logger.warning("tool %s: arguments are not valid JSON: %.200s", pending.name, raw)
        return {"__parse_error__": raw[:2000]}
    if not isinstance(parsed, dict):
        return {"__parse_error__": raw[:2000]}
    return parsed
