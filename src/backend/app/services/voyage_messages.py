"""任务对话流：用户与任务 agent 的双向消息（不 import fastapi）。

- append_message：流内 seq 定序追加（max+1；唯一约束兜底并发，冲突重试）。
- pending_chat_messages / mark_chat_consumed：非阻塞用户建议的排队与消费。
  消费标记必须与「建议生效的那次落库」同一事务提交（调用方 commit），
  这样 resume 重放不会重复注入——建议要么随效果一起生效，要么原样留在队列里。
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.voyage import VoyageMessage

_APPEND_RETRIES = 3
TEXT_MAX_CHARS = 8_000


class MessageSeqConflictError(Exception):
    """并发追加连续撞 seq 唯一约束（重试耗尽）。"""


def serialize_message(message: VoyageMessage) -> dict[str, Any]:
    """SSE `message` 事件与 API 共用的消息序列化。"""
    return {
        "id": str(message.id),
        "run_id": str(message.run_id),
        "seq": message.seq,
        "role": message.role,
        "kind": message.kind,
        "text": message.text,
        "payload": message.payload,
        "status": message.status,
        "reply_to": str(message.reply_to) if message.reply_to else None,
        "step_id": str(message.step_id) if message.step_id else None,
        "consumed_at": message.consumed_at.isoformat() if message.consumed_at else None,
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


async def append_message(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    role: str,
    kind: str,
    text: str,
    author_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
    status: str = "none",
    reply_to: uuid.UUID | None = None,
    step_id: uuid.UUID | None = None,
) -> VoyageMessage:
    """追加一条消息并 commit；返回持久化行。

    seq = 当前最大值 + 1；用户发帖与引擎播报可能并发，撞唯一约束就重算重试。
    注意：本函数自己 commit（seq 分配需要独立提交点），调用方若有未提交状态
    应先行 commit 或改用独立 session。
    """
    last_error: IntegrityError | None = None
    for _ in range(_APPEND_RETRIES):
        next_seq = (
            await session.execute(
                select(VoyageMessage.seq)
                .where(VoyageMessage.run_id == run_id)
                .order_by(VoyageMessage.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        message = VoyageMessage(
            run_id=run_id,
            seq=(next_seq or 0) + 1,
            role=role,
            author_id=author_id,
            kind=kind,
            text=text[:TEXT_MAX_CHARS],
            payload=payload,
            status=status,
            reply_to=reply_to,
            step_id=step_id,
        )
        session.add(message)
        try:
            await session.commit()
        except IntegrityError as e:
            await session.rollback()
            last_error = e
            continue
        await session.refresh(message)
        return message
    raise MessageSeqConflictError(str(run_id)) from last_error


async def list_messages(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    after_seq: int | None = None,
    limit: int = 500,
) -> list[VoyageMessage]:
    stmt = (
        select(VoyageMessage)
        .where(VoyageMessage.run_id == run_id)
        .order_by(VoyageMessage.seq)
        .limit(limit)
    )
    if after_seq is not None:
        stmt = stmt.where(VoyageMessage.seq > after_seq)
    return list((await session.execute(stmt)).scalars().all())


async def pending_chat_messages(session: AsyncSession, run_id: uuid.UUID) -> list[VoyageMessage]:
    """未被 agent 消费的用户建议，按 seq 升序。"""
    stmt = (
        select(VoyageMessage)
        .where(
            VoyageMessage.run_id == run_id,
            VoyageMessage.kind == "chat",
            VoyageMessage.consumed_at.is_(None),
        )
        .order_by(VoyageMessage.seq)
    )
    return list((await session.execute(stmt)).scalars().all())


def mark_chat_consumed(messages: list[VoyageMessage], *, step_id: uuid.UUID | None = None) -> None:
    """标记建议已消费。只改内存对象，**由调用方与效果同一事务 commit**。"""
    now = utcnow()
    for message in messages:
        message.consumed_at = now
        message.consumed_step_id = step_id


def guidance_text(messages: list[VoyageMessage]) -> str:
    """把待消费建议拼成注入 prompt / params 的纯文本（一行一条）。"""
    return "\n".join(f"- {m.text}" for m in messages)


# ---- ask（agent 向用户提问）----


async def open_ask(session: AsyncSession, run_id: uuid.UUID) -> VoyageMessage | None:
    """当前等回答的提问（最多一个活跃；防御性取最新一条）。"""
    stmt = (
        select(VoyageMessage)
        .where(
            VoyageMessage.run_id == run_id,
            VoyageMessage.kind == "ask",
            VoyageMessage.status == "open",
        )
        .order_by(VoyageMessage.seq.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_open_ask(
    session: AsyncSession, run_id: uuid.UUID, *, ask_kind: str, step_id: uuid.UUID | None
) -> VoyageMessage | None:
    """幂等锚点：同 run 同 (ask_kind, step_id) 已有 open 提问则复用，不重复插。"""
    stmt = select(VoyageMessage).where(
        VoyageMessage.run_id == run_id,
        VoyageMessage.kind == "ask",
        VoyageMessage.status == "open",
        VoyageMessage.step_id == step_id,
    )
    for m in (await session.execute(stmt)).scalars().all():
        if (m.payload or {}).get("ask_kind") == ask_kind:
            return m
    return None


async def answered_asks(session: AsyncSession, run_id: uuid.UUID) -> list[VoyageMessage]:
    """已回答、等引擎消费的提问，按 seq 升序。"""
    stmt = (
        select(VoyageMessage)
        .where(
            VoyageMessage.run_id == run_id,
            VoyageMessage.kind == "ask",
            VoyageMessage.status == "answered",
        )
        .order_by(VoyageMessage.seq)
    )
    return list((await session.execute(stmt)).scalars().all())


async def answer_of(session: AsyncSession, ask: VoyageMessage) -> VoyageMessage | None:
    """某提问的（最新一条）回答。"""
    stmt = (
        select(VoyageMessage)
        .where(VoyageMessage.reply_to == ask.id, VoyageMessage.kind == "answer")
        .order_by(VoyageMessage.seq.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def supersede_open_asks(session: AsyncSession, run_id: uuid.UUID) -> int:
    """把所有 open 提问置 superseded（cancel / 新提问覆盖旧提问时用）。

    只改内存对象并 flush，由调用方 commit。返回受影响条数。
    """
    stmt = select(VoyageMessage).where(
        VoyageMessage.run_id == run_id,
        VoyageMessage.kind == "ask",
        VoyageMessage.status == "open",
    )
    rows = list((await session.execute(stmt)).scalars().all())
    for m in rows:
        m.status = "superseded"
    return len(rows)
