"""对话会话的读写。

``replay()`` 是**唯一**把数据库行翻回 ``Message`` 的地方——压缩、图片降级、块的还原
都收在这里，别在调用方各写一份。
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm.base import (
    ContentBlock,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from app.models.conversation import Conversation, ConversationMessage

#: 标题取首条用户消息的前若干字符
_TITLE_CHARS = 60


def blocks_to_json(blocks: tuple[ContentBlock, ...] | list[ContentBlock]) -> list[dict[str, Any]]:
    """块 → 可落库的 JSON（provider 中立）。

    **图片不落 base64**。一次取图工具能返回好几张，二十轮对话的 JSON 就爆了；而且
    存了也没用——回放时喂给模型的历史带着几十张图，token 立刻见底。这里只留一行占位，
    模型对"历史里少张图"的容忍度远高于"历史里有个截断的 base64"。
    """
    out: list[dict[str, Any]] = []
    for b in blocks:
        if isinstance(b, TextBlock):
            out.append({"kind": "text", "text": b.text})
        elif isinstance(b, ThinkingBlock):
            out.append({"kind": "thinking", "text": b.text, "signature": b.signature})
        elif isinstance(b, ToolUseBlock):
            out.append({"kind": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif isinstance(b, ToolResultBlock):
            out.append(
                {
                    "kind": "tool_result",
                    "tool_use_id": b.tool_use_id,
                    "content": b.content,
                    "is_error": b.is_error,
                    "images": len(b.images),  # 只记数量，见上
                }
            )
        else:  # ImageBlock
            out.append({"kind": "image", "mime": b.mime, "label": b.label})
    return out


def blocks_from_json(raw: list[dict[str, Any]] | None) -> list[ContentBlock]:
    """JSON → 块。未知 kind 一律忽略（存量数据与将来的新块类型都不该炸这里）。"""
    blocks: list[ContentBlock] = []
    for item in raw or []:
        kind = item.get("kind")
        if kind == "text":
            blocks.append(TextBlock(item.get("text") or ""))
        elif kind == "thinking":
            blocks.append(ThinkingBlock(item.get("text") or "", item.get("signature")))
        elif kind == "tool_use":
            blocks.append(
                ToolUseBlock(
                    str(item.get("id") or ""), str(item.get("name") or ""), item.get("input") or {}
                )
            )
        elif kind == "tool_result":
            note = ""
            if count := int(item.get("images") or 0):
                note = f"\n（另有 {count} 张图片，历史回放中已省略）"
            blocks.append(
                ToolResultBlock(
                    str(item.get("tool_use_id") or ""),
                    (item.get("content") or "") + note,
                    is_error=bool(item.get("is_error")),
                )
            )
        elif kind == "image":
            blocks.append(TextBlock(f"[图片 {item.get('label') or item.get('mime') or ''}]"))
    return blocks


async def get_or_create(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    scope_kind: str,
    scope_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
) -> Conversation:
    """拿到会话；``conversation_id`` 给了就按它取（**并校验归属**），否则新建。"""
    if conversation_id is not None:
        conv = await session.get(Conversation, conversation_id)
        if conv is not None and conv.user_id == user_id:
            return conv
        # 归属对不上按不存在处理，另开一条——别把别人的会话续下去
    conv = Conversation(
        user_id=user_id,
        scope_kind=scope_kind,
        scope_id=scope_id,
        project_id=project_id,
        title="",
        settings={},
        usage={},
    )
    session.add(conv)
    await session.flush()
    return conv


async def append_message(
    session: AsyncSession,
    *,
    conversation: Conversation,
    role: str,
    blocks: tuple[ContentBlock, ...] | list[ContentBlock] | None = None,
    text: str | None = None,
    kind: str = "normal",
    sources: list[Any] | None = None,
    usage: dict[str, Any] | None = None,
    model: str | None = None,
    stop_reason: str | None = None,
    status: str = "complete",
    error: str | None = None,
) -> ConversationMessage:
    """追加一条消息。``seq`` 在会话内自增（唯一约束兜底并发）。"""
    blocks = list(blocks or ([TextBlock(text)] if text else []))
    flat = text if text is not None else Message(role=role, content=blocks).text
    next_seq = (
        await session.scalar(
            select(func.coalesce(func.max(ConversationMessage.seq), -1) + 1).where(
                ConversationMessage.conversation_id == conversation.id
            )
        )
    ) or 0
    row = ConversationMessage(
        conversation_id=conversation.id,
        seq=int(next_seq),
        role=role,
        kind=kind,
        blocks=blocks_to_json(blocks),
        text=flat,
        status=status,
        sources=sources,
        usage=usage,
        model=model,
        stop_reason=stop_reason,
        error=error,
    )
    session.add(row)
    conversation.last_message_at = datetime.now(UTC)
    if not conversation.title and role == "user" and flat:
        conversation.title = flat.strip()[:_TITLE_CHARS]
    if usage:
        acc = dict(conversation.usage or {})
        for key in ("prompt_tokens", "completion_tokens"):
            acc[key] = int(acc.get(key, 0)) + int(usage.get(key, 0) or 0)
        acc["total_tokens"] = int(acc.get("prompt_tokens", 0)) + int(
            acc.get("completion_tokens", 0)
        )
        conversation.usage = acc
    await session.flush()
    return row


async def replay(
    session: AsyncSession, *, conversation_id: uuid.UUID, limit: int | None = None
) -> list[Message]:
    """把会话历史翻回 ``Message`` 列表，按 seq 升序。

    只回放 ``complete`` 的消息：中断/出错那条留在库里给人看，但不该喂回给模型——
    半截的 assistant 轮会让它以为自己说过那些话。
    """
    stmt = (
        select(ConversationMessage)
        .where(
            ConversationMessage.conversation_id == conversation_id,
            ConversationMessage.status == "complete",
        )
        .order_by(ConversationMessage.seq.desc())
    )
    if limit:
        stmt = stmt.limit(limit)
    rows = list((await session.execute(stmt)).scalars().all())
    rows.reverse()
    return [Message(role=r.role, content=blocks_from_json(r.blocks)) for r in rows]


async def finish_message(
    session: AsyncSession,
    *,
    message: ConversationMessage,
    blocks: tuple[ContentBlock, ...] | list[ContentBlock] | None = None,
    status: str = "complete",
    usage: dict[str, Any] | None = None,
    model: str | None = None,
    stop_reason: str | None = None,
    error: str | None = None,
) -> ConversationMessage:
    """收尾一条 streaming 中的消息（正常结束 / 被中断 / 出错都走它）。"""
    if blocks is not None:
        message.blocks = blocks_to_json(blocks)
        message.text = Message(role=message.role, content=list(blocks)).text
    message.status = status
    if usage:
        message.usage = usage
    if model:
        message.model = model
    if stop_reason:
        message.stop_reason = stop_reason
    if error:
        message.error = error
    await session.flush()
    return message


async def list_conversations(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    scope_kind: str | None = None,
    scope_id: uuid.UUID | None = None,
    limit: int = 30,
) -> list[Conversation]:
    stmt = select(Conversation).where(
        Conversation.user_id == user_id, Conversation.status == "active"
    )
    if scope_kind:
        stmt = stmt.where(Conversation.scope_kind == scope_kind)
    if scope_id is not None:
        stmt = stmt.where(Conversation.scope_id == scope_id)
    stmt = stmt.order_by(Conversation.last_message_at.desc().nullslast()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())
