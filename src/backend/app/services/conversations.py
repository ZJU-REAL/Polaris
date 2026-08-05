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

#: 回放预算的保守缺省（字符）。管理端没在模型路由上填 context_window 时用它——
#: 约合 16k token，给工具 schema、系统提示和本轮生成留足余量。
DEFAULT_REPLAY_BUDGET_CHARS = 64_000

#: 旧轮次里单个工具结果保留多少。历史里的工具结果是 token 大头（一条 4000 字符），
#: 而模型早已基于它作过答——保留一行出处就够了。
_OLD_RESULT_KEEP_CHARS = 200


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


async def get_owned(
    session: AsyncSession, *, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> Conversation | None:
    """取自己的那场对话；不是自己的返回 None（调用方一律 404，不给 403——
    403 等于承认这个 id 存在）。"""
    conv = await session.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user_id:
        return None
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
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    limit: int | None = None,
    budget_chars: int | None = None,
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
    messages = [Message(role=r.role, content=blocks_from_json(r.blocks)) for r in rows]
    return trim_history(messages, budget_chars=budget_chars)


def _is_turn_start(msg: Message) -> bool:
    """真正的用户提问（而不是装工具结果的 user 消息）——轮次从这里开始。"""
    if msg.role != "user":
        return False
    blocks = msg.content
    if isinstance(blocks, str):
        return True
    return not any(isinstance(b, ToolResultBlock) for b in blocks)


def trim_history(
    messages: list[Message], *, budget_chars: int | None = None
) -> list[Message]:
    """把历史裁进预算。两条硬规则：

    1. **只在轮次边界裁**。一轮 = 一个用户提问到下一个提问之间的全部消息（含中间的
       tool_use / tool_result 往返）。从半中腰切开会把 assistant 的 tool_use 和它的
       tool_result 拆散——Anthropic 对孤儿 tool_use 直接 400，OpenAI 侧行为未定义。
    2. **保留的旧轮次里，工具结果压成一行**。模型早已基于它作过答，整段留着只是
       每轮重发的死重；最近一轮保留原文（追问经常要引用它）。

    预算按拍平字符数算（与 estimate_tokens 的 len/4 同源），不精确但方向正确：
    这里要防的是无界增长，不是精确计费。
    """
    budget = budget_chars or DEFAULT_REPLAY_BUDGET_CHARS
    if not messages:
        return messages

    # 切成轮次（首条消息之前若有游离的 assistant 消息，归入第一轮）
    turns: list[list[Message]] = []
    current: list[Message] = []
    for msg in messages:
        if _is_turn_start(msg) and current:
            turns.append(current)
            current = []
        current.append(msg)
    if current:
        turns.append(current)

    # 从最新往回收轮次，直到预算耗尽；最新一轮永远保留（哪怕它自己就超了预算）
    kept: list[list[Message]] = []
    used = 0
    for index, turn in enumerate(reversed(turns)):
        is_latest = index == 0
        turn = turn if is_latest else [_shrink_old_results(m) for m in turn]
        size = sum(len(m.text) for m in turn)
        if not is_latest and used + size > budget:
            break
        kept.append(turn)
        used += size
    kept.reverse()
    return [msg for turn in kept for msg in turn]


def _shrink_old_results(msg: Message) -> Message:
    """旧轮次里的工具结果压成一行出处。"""
    blocks = msg.content
    if isinstance(blocks, str):
        return msg
    changed = False
    out: list[ContentBlock] = []
    for b in blocks:
        if isinstance(b, ToolResultBlock) and len(b.content) > _OLD_RESULT_KEEP_CHARS:
            out.append(
                ToolResultBlock(
                    b.tool_use_id,
                    b.content[:_OLD_RESULT_KEEP_CHARS] + "…（历史轮次，已截断）",
                    is_error=b.is_error,
                )
            )
            changed = True
        else:
            out.append(b)
    return Message(role=msg.role, content=out) if changed else msg


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
