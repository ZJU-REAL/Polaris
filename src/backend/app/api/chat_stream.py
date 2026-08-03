"""文献对话的 SSE 骨架：先发 ``sources``，再流式 ``delta``* → ``done``，出错 ``error`` 后关流。

这段骨架此前有**三份逐字相同的副本**（wiki.py / papers.py / libraries.py），daily.py 还
从 wiki.py 反向 import 了一份来用。三份各自演化的结果是行为已经不一致：只有 papers.py
带了 ``X-Accel-Buffering: no``，另外两个在 nginx 后面会被缓冲，流式变成一次性吐出。

所以先收敛再谈扩展。这个模块只做搬迁，不改任何行为。
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict
from typing import Any

from fastapi.responses import StreamingResponse

from app.core.llm.fake import estimate_tokens
from app.core.llm.router import get_llm_router

logger = logging.getLogger(__name__)

#: 空闲多久发一次注释心跳。要短于反向代理与浏览器的空闲超时，否则长思考会被判死。
HEARTBEAT_SECONDS = 15.0

#: 流式响应头。``X-Accel-Buffering: no`` 是给 nginx 看的——不关缓冲，它会攒够一整块
#: 才下发，用户看到的就是"转很久然后一次性蹦出全文"。
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def sse_frame(event: str, data: Any) -> str:
    """一帧 SSE。``default=str`` 不能省：payload 里常有 UUID / datetime，没有它会抛
    TypeError 把整条流打断，而不是序列化成字符串。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def chat_stream_response(
    messages: Sequence[Any],
    sources: Sequence[Any],
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID | None,
    stage: str = "reading",
    log_label: str = "literature chat",
    always_send_sources: bool = True,
) -> StreamingResponse:
    """把一组消息按 SSE 流式吐出去。

    ``always_send_sources``：AI 伴读只在真的有参考文献时才发 ``sources``（它的语料就是
    当前这一篇，没有"引用来源"可言），其余入口恒发一帧——前端据此渲染来源清单，
    收不到就一直显示加载态。
    """
    llm = get_llm_router()

    async def event_stream() -> AsyncIterator[str]:
        if sources or always_send_sources:
            yield sse_frame("sources", {"items": [asdict(s) for s in sources]})
        queue: asyncio.Queue[tuple[str, str | None]] = asyncio.Queue()

        async def pump() -> None:
            try:
                # router.stream 结束时自动写 LLMUsage 记账（归属 user + project）
                async for chunk in llm.stream(
                    stage, messages, user_id=user_id, project_id=project_id
                ):
                    await queue.put(("delta", chunk))
                await queue.put(("done", None))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — 转成 error 事件后关流
                logger.warning("%s stream failed", log_label, exc_info=True)
                await queue.put(("error", f"{type(e).__name__}: {e}"))

        task = asyncio.create_task(pump())
        collected: list[str] = []
        try:
            while True:
                try:
                    kind, payload = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                if kind == "delta":
                    collected.append(payload or "")
                    yield sse_frame("delta", {"text": payload})
                elif kind == "done":
                    # usage 与 router 记账口径一致（provider 无 usage 时按 len/4 估算）
                    usage = {
                        "prompt_tokens": sum(estimate_tokens(m.text) for m in messages),
                        "completion_tokens": estimate_tokens("".join(collected)),
                    }
                    yield sse_frame("done", {"usage": usage})
                    return
                else:  # error
                    yield sse_frame("error", {"detail": payload})
                    return
        finally:
            task.cancel()

    return StreamingResponse(
        event_stream(), media_type="text/event-stream", headers=dict(SSE_HEADERS)
    )
