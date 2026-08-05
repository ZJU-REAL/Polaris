"""Helm（掌舵 · execute）：把模型要求的工具调用真的跑起来。

一条纪律压倒一切：**异常绝不外抛**。工具炸了、超时了、参数不是合法 JSON、工具名根本
不存在——全部转成结果回喂给模型，让它自己纠正。一次工具失败不该让整轮对话死掉；
用户等了十几秒，最差也该收到一句「这个没查着，我换个说法再试」。

只读工具并行跑（信号量封顶）。写工具本期没有，将来也必须串行：没有事务包裹，并行改
同一行就是竞态，而且审批 UI 一次弹两个框是灾难。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

from app.agents.chat.events import ToolResultEvent
from app.core.llm.base import ToolResultBlock, ToolUseBlock
from app.tools.context import ToolContext
from app.tools.registry import get_tool, result_images, result_payload, run_tool

logger = logging.getLogger(__name__)

#: 同时最多跑几个工具。只读工具之间没有依赖，但并发太高会把数据库连接池吃光。
TOOL_CONCURRENCY = 4

#: 单个工具的墙钟上限。卡住的工具会让整轮对话跟着卡住，用户只看到一直转圈。
TOOL_TIMEOUT_SECONDS = 60.0

#: 回喂给模型的单个工具结果上限。超过就截断，完整内容留在库里按需取。
RESULT_CHARS = 4000

#: 推给前端的预览上限。比回喂的更短——它只是让人看一眼「查到了什么」。
PREVIEW_CHARS = 800


class Helm:
    def __init__(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    async def run(
        self, calls: list[ToolUseBlock]
    ) -> AsyncIterator[tuple[ToolResultEvent, ToolResultBlock]]:
        """并行执行一轮里的工具调用，按**发起顺序**把结果吐出去。

        顺序按发起而不是按完成：界面上卡片的次序应该与模型的意图一致，谁先跑完是
        实现细节，不该影响读者理解它在做什么。
        """
        semaphore = asyncio.Semaphore(TOOL_CONCURRENCY)

        async def one(call: ToolUseBlock) -> tuple[ToolResultEvent, ToolResultBlock]:
            async with semaphore:
                return await self.execute(call)

        for coro in [asyncio.create_task(one(c)) for c in calls]:
            event, block = await coro
            yield event, block

    async def execute(self, call: ToolUseBlock) -> tuple[ToolResultEvent, ToolResultBlock]:
        started = time.monotonic()
        spec = get_tool(call.name)
        if spec is None:
            # 未知工具不打断循环：把情况告诉模型，它下一轮自己纠正
            return self._failure(call, {"error": f"未知工具 {call.name!r}"}, started)
        if "__parse_error__" in call.input:
            # 弱模型上高频：参数流到一半坏掉。单独成一类，因为提示不同——它该重发这次
            # 调用，而不是换个工具。
            return self._failure(
                call, {"error": "参数不是合法 JSON，请重新发起这次调用"}, started
            )
        try:
            result = await asyncio.wait_for(
                run_tool(self._ctx, call.name, call.input), timeout=TOOL_TIMEOUT_SECONDS
            )
        except TimeoutError:
            return self._failure(
                call, {"error": f"工具执行超过 {TOOL_TIMEOUT_SECONDS:.0f} 秒"}, started
            )
        except asyncio.CancelledError:
            raise  # 取消要往上传，否则断连时这里会把它吞掉
        except Exception as e:  # noqa: BLE001 — 工具异常转成结果回喂，不打断循环
            logger.warning("chat agent: tool %s failed", call.name, exc_info=True)
            return self._failure(call, {"error": f"{type(e).__name__}: {e}"}, started)

        # 序列化也在 try 里：这一步同样会炸。生产上就是这么断的——get_project_status
        # 的返回里带 datetime，json.dumps 抛 TypeError，而那行当时在 try 之外，于是异常
        # 穿出循环、带走 SSE 连接，用户只看到一句「network error」。**「异常绝不外抛」
        # 必须覆盖到把结果交出去为止**，否则这条保证在最后一米失效。
        try:
            payload = result_payload(result)
            images = result_images(result)
            # default=str：工具的返回里有 datetime / UUID / Decimal 是常态，不该由每个
            # 工具各自记得转成字符串——漏一个就是一次断流。
            text = json.dumps(payload, ensure_ascii=False, default=str)[:RESULT_CHARS]
        except Exception as e:  # noqa: BLE001
            logger.warning("chat agent: tool %s result not serializable", call.name, exc_info=True)
            return self._failure(
                call, {"error": f"结果无法序列化：{type(e).__name__}: {e}"}, started
            )
        return (
            ToolResultEvent(
                id=call.id,
                name=call.name,
                ok=True,
                summary=spec.summary(call.input, payload),
                preview=text[:PREVIEW_CHARS],
                duration_ms=int((time.monotonic() - started) * 1000),
                images=len(images),
                image_refs=tuple(
                    {**img.ref, "label": img.label} for img in images if img.ref is not None
                ),
            ),
            ToolResultBlock(call.id, text),
        )

    def _failure(
        self, call: ToolUseBlock, payload: dict[str, Any], started: float
    ) -> tuple[ToolResultEvent, ToolResultBlock]:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        return (
            ToolResultEvent(
                id=call.id,
                name=call.name,
                ok=False,
                summary=str(payload.get("error", "失败")),
                preview=text[:PREVIEW_CHARS],
                duration_ms=int((time.monotonic() - started) * 1000),
            ),
            ToolResultBlock(call.id, text, is_error=True),
        )
