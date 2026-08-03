"""全局助手：Claude Code 式的多轮工具循环，走 SSE。

**新路径，六个老对话端点一行不动。** 助手成熟之前两套并存，出问题随时关掉开关退回去。

事件是加法式扩展的：正文仍然走 ``delta``，形状与老端点一字不差，所以只认
``sources/delta/done/error`` 的客户端也能工作，只是看不到工具卡片。
"""

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import asdict, is_dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.chat.events import (
    ChatEvent,
    CompactionEvent,
    DeltaEvent,
    DoneEvent,
    ErrorEvent,
    MetaEvent,
    SourcesEvent,
    ThinkingEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageEvent,
)
from app.agents.chat.loop import ChatAgentLoop, ChatTurnRequest
from app.agents.chat.prompt import DEFAULT_TOOL_NAMES
from app.api.auth import current_active_user, require_llm_chat
from app.core.config import get_settings
from app.core.db import get_session
from app.core.llm.base import TextBlock
from app.core.llm.router import get_llm_router
from app.models.user import User
from app.schemas.chat_agent import (
    ConversationCreate,
    ConversationRead,
    ConversationTurnRequest,
    MessageRead,
)
from app.services import conversations as store
from app.tools.context import ToolContext

router = APIRouter(prefix="/chat", tags=["chat"])

#: 事件类 → 线上帧名。加法式：老客户端只认识其中四个，其余忽略即可。
_EVENT_NAMES: dict[type, str] = {
    MetaEvent: "meta",
    SourcesEvent: "sources",
    DeltaEvent: "delta",
    ThinkingEvent: "thinking",
    ToolCallEvent: "tool_call",
    ToolResultEvent: "tool_result",
    UsageEvent: "usage",
    CompactionEvent: "compaction",
    DoneEvent: "done",
    ErrorEvent: "error",
}


def _frame(event: str, data: Any) -> str:
    # NOTE: #252 合并后改为从 app.api.chat_stream import sse_frame，这里不再自带一份
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def _to_frame(ev: ChatEvent) -> str:
    name = _EVENT_NAMES.get(type(ev), "unknown")
    payload = asdict(ev) if is_dataclass(ev) else {}
    return _frame(name, payload)


def _require_enabled() -> None:
    if not get_settings().chat_agent_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CHAT_AGENT_DISABLED")


@router.post("/conversations", response_model=ConversationRead, status_code=201)
async def create_conversation(
    payload: ConversationCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> ConversationRead:
    _require_enabled()
    conv = await store.get_or_create(
        session,
        user_id=user.id,
        scope_kind=payload.scope_kind,
        scope_id=payload.scope_id,
        project_id=payload.project_id,
    )
    await session.commit()
    return ConversationRead.model_validate(conv, from_attributes=True)


@router.get("/conversations", response_model=list[ConversationRead])
async def list_conversations(
    scope_kind: str | None = Query(default=None),
    scope_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[ConversationRead]:
    _require_enabled()
    rows = await store.list_conversations(
        session, user_id=user.id, scope_kind=scope_kind, scope_id=scope_id, limit=limit
    )
    return [ConversationRead.model_validate(r, from_attributes=True) for r in rows]


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageRead])
async def list_messages(
    conversation_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[MessageRead]:
    """会话的完整消息（含工具块）。前端展开"看完整结果"时用它——SSE 里的 preview 是截断的。"""
    _require_enabled()
    from sqlalchemy import select

    from app.models.conversation import Conversation, ConversationMessage

    conv = await session.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CONVERSATION_NOT_FOUND")
    rows = (
        (
            await session.execute(
                select(ConversationMessage)
                .where(ConversationMessage.conversation_id == conversation_id)
                .order_by(ConversationMessage.seq)
            )
        )
        .scalars()
        .all()
    )
    return [MessageRead.model_validate(r, from_attributes=True) for r in rows]


@router.post("/conversations/{conversation_id}/turn")
async def run_turn(
    conversation_id: uuid.UUID,
    payload: ConversationTurnRequest,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_llm_chat),
) -> StreamingResponse:
    """跑一轮：模型可以反复调工具，最后给出回答。"""
    _require_enabled()
    conv = await store.get_or_create(
        session, user_id=user.id, scope_kind="global", conversation_id=conversation_id
    )
    if conv.id != conversation_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CONVERSATION_NOT_FOUND")
    history = await store.replay(session, conversation_id=conv.id)
    await store.append_message(session, conversation=conv, role="user", text=payload.question)
    await session.commit()

    project_id = conv.project_id or conv.scope_id or uuid.uuid4()
    tool_ctx = ToolContext(project_id=project_id, llm=get_llm_router(), user_id=user.id)
    loop = ChatAgentLoop(llm=get_llm_router(), tool_ctx=tool_ctx, history=history)
    req = ChatTurnRequest(
        conversation_id=conv.id,
        question=payload.question,
        tool_names=tuple(payload.tool_names or DEFAULT_TOOL_NAMES),
        max_rounds=payload.max_rounds,
        statement=payload.statement,
    )
    conv_id, user_id = conv.id, user.id

    async def event_stream() -> AsyncIterator[str]:
        collected: list[str] = []
        stop_reason, usage = "stop", {}
        try:
            async for ev in loop.run(req):
                if isinstance(ev, DeltaEvent):
                    collected.append(ev.text)
                elif isinstance(ev, DoneEvent):
                    stop_reason, usage = ev.stop_reason, ev.usage
                yield _to_frame(ev)
        finally:
            # 断线也要落库：把已经生成的部分留住，标成 interrupted。
            # shield 是必需的——取消态下再 await 数据库写会被二次取消。
            import asyncio

            text = "".join(collected)
            if text:
                await asyncio.shield(
                    _persist_answer(conv_id, user_id, text, stop_reason, usage)
                )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _persist_answer(
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    text: str,
    stop_reason: str,
    usage: dict[str, Any],
) -> None:
    """把这一轮的回答落库。用独立 session：请求那个可能已经随连接一起没了。"""
    from app.core.db import get_sessionmaker

    async with get_sessionmaker()() as session:
        conv = await store.get_or_create(
            session, user_id=user_id, scope_kind="global", conversation_id=conversation_id
        )
        await store.append_message(
            session,
            conversation=conv,
            role="assistant",
            blocks=[TextBlock(text)],
            usage=usage or None,
            stop_reason=stop_reason,
            status="complete" if stop_reason != "interrupted" else "interrupted",
        )
        await session.commit()
