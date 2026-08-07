"""Voyage 路由（docs/api-m1.md §3）：创建入队 / 列表 / 详情 / 取消 / SSE 事件流。"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.api.chat_stream import sse_frame as _sse_frame
from app.core.db import get_session
from app.core.events import EventBus, get_event_bus, voyage_channel
from app.core.queue import TaskQueue, get_task_queue
from app.core.redis import get_redis_dep
from app.models.user import User
from app.models.voyage import TERMINAL_STATUSES, VoyageMessage, VoyageRun
from app.schemas.voyage import (
    VoyageAskAnswer,
    VoyageCreate,
    VoyageDetailRead,
    VoyageMessageCreate,
    VoyageMessageRead,
    VoyagePlanEvent,
    VoyageRead,
    VoyageSkillUse,
    VoyageTerminalLogRead,
)
from app.services import experiments as experiments_service
from app.services import projects as projects_service
from app.services import voyage_messages as messages_service
from app.services import voyages as voyages_service

router = APIRouter(prefix="/voyages", tags=["voyages"])

_HEARTBEAT_SECONDS = 15.0


async def _get_owned_voyage(
    session: AsyncSession, voyage_id: uuid.UUID, user: User, with_steps: bool = False
) -> VoyageRun:
    run = await voyages_service.get_voyage(
        session, voyage_id=voyage_id, user_id=user.id, with_steps=with_steps, user=user
    )
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="VOYAGE_NOT_FOUND")
    return run


@router.post("", response_model=VoyageRead, status_code=status.HTTP_201_CREATED)
async def create_voyage(
    data: VoyageCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
) -> VoyageRead:
    project = await projects_service.get_project(
        session, project_id=data.project_id, user_id=user.id
    )
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="PROJECT_NOT_FOUND")
    run = await voyages_service.create_voyage(session, created_by=user.id, data=data)
    await queue.enqueue("run_voyage", str(run.id))
    return VoyageRead.model_validate(run)


@router.get("", response_model=list[VoyageRead])
async def list_voyages(
    project_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[VoyageRead]:
    runs = await voyages_service.list_voyages(session, user_id=user.id, project_id=project_id)
    return [VoyageRead.model_validate(r) for r in runs]


def _skills_summary(run: VoyageRun) -> list[VoyageSkillUse]:
    """checkpoint["skills"] 快照 → 摘要列表（详情页「本次任务使用的技能」）。"""
    snapshot = (run.checkpoint or {}).get("skills")
    if not isinstance(snapshot, dict):
        return []
    out: list[VoyageSkillUse] = []
    for target, entries in snapshot.items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if isinstance(e, dict) and e.get("slug"):
                out.append(
                    VoyageSkillUse(
                        slug=str(e["slug"]),
                        name=str(e.get("name") or e["slug"]),
                        kind=str(e.get("kind") or ""),
                        version=int(e.get("version") or 0),
                        target=str(target),
                    )
                )
    return out


@router.get("/{voyage_id}", response_model=VoyageDetailRead)
async def get_voyage(
    voyage_id: uuid.UUID,
    include_obsolete: bool = Query(default=False),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> VoyageDetailRead:
    run = await _get_owned_voyage(session, voyage_id, user, with_steps=True)
    detail = VoyageDetailRead.model_validate(run)
    # 默认只回当前活动清单：计划调整时被作废的步骤（obsolete）留痕在库，
    # include_obsolete=true 才随详情返回（任务板的"显示已作废步骤"开关）
    if not include_obsolete:
        detail.steps = [s for s in detail.steps if s.status != "obsolete"]
    detail.skills = _skills_summary(run)
    history = (run.checkpoint or {}).get("plan_history")
    if isinstance(history, list):
        detail.plan_history = [
            VoyagePlanEvent.model_validate(e) for e in history if isinstance(e, dict)
        ]
    open_ask = await messages_service.open_ask(session, run.id)
    if open_ask is not None:
        detail.open_ask = VoyageMessageRead.model_validate(open_ask)
    return detail


@router.get("/{voyage_id}/logs", response_model=list[VoyageTerminalLogRead])
async def get_voyage_logs(
    voyage_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[VoyageTerminalLogRead]:
    """任务终端历史日志（结构化日志行 + 大模型完整输出），供刷新后 / 事后回看。"""
    await _get_owned_voyage(session, voyage_id, user)
    from app.services.voyage_logs import fetch_terminal_logs

    rows = await fetch_terminal_logs(session, voyage_id)
    return [VoyageTerminalLogRead.model_validate(r) for r in rows]


@router.get("/{voyage_id}/messages", response_model=list[VoyageMessageRead])
async def list_voyage_messages(
    voyage_id: uuid.UUID,
    after_seq: int | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=1000),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[VoyageMessageRead]:
    """任务对话流（用户建议 / agent 提问与播报），按 seq 升序。"""
    await _get_owned_voyage(session, voyage_id, user)
    rows = await messages_service.list_messages(
        session, voyage_id, after_seq=after_seq, limit=limit
    )
    return [VoyageMessageRead.model_validate(r) for r in rows]


@router.post(
    "/{voyage_id}/messages",
    response_model=VoyageMessageRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_voyage_message(
    voyage_id: uuid.UUID,
    data: VoyageMessageCreate,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    bus: EventBus = Depends(get_event_bus),
) -> VoyageMessageRead:
    """给运行中的任务发一条建议（非阻塞：agent 在下一个决策点参考）。

    终态任务没有下一个决策点，409；暂停中的任务可以发（恢复后消费）。
    """
    run = await _get_owned_voyage(session, voyage_id, user)
    if run.status in TERMINAL_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="VOYAGE_ALREADY_FINISHED")
    message = await messages_service.append_message(
        session,
        run.id,
        role="user",
        kind="chat",
        text=data.text,
        author_id=user.id,
    )
    await bus.publish_voyage_event(
        run.id, "message", {"message": messages_service.serialize_message(message)}
    )
    return VoyageMessageRead.model_validate(message)


@router.post(
    "/{voyage_id}/asks/{message_id}/answer",
    response_model=VoyageMessageRead,
    status_code=status.HTTP_201_CREATED,
)
async def answer_voyage_ask(
    voyage_id: uuid.UUID,
    message_id: uuid.UUID,
    data: VoyageAskAnswer,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
    bus: EventBus = Depends(get_event_bus),
) -> VoyageMessageRead:
    """回答 AI 的提问并恢复任务。

    - ``choice='abort'``：人拍板放弃 → 任务 failed（唯一由用户决定的失败路径）；
    - 其余：回答落库、文本镜像成一条建议消息（引擎在下一个决策点消费）、
      确定性即时效果（如追加预算）、paused_ask → executing 并入队续跑。
    - 重复回答（并发双答）由 ask 行的条件 UPDATE 挡住，409。
    """
    run = await _get_owned_voyage(session, voyage_id, user)
    ask = await session.get(VoyageMessage, message_id)
    if ask is None or ask.run_id != run.id or ask.kind != "ask":
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="ASK_NOT_FOUND")
    text = data.text.strip()
    if not text and not data.choice:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail="ANSWER_EMPTY"
        )
    # 条件 UPDATE 防并发双答（两个成员同时点回答只有一个生效）
    result = await session.execute(
        sa_update(VoyageMessage)
        .where(VoyageMessage.id == ask.id, VoyageMessage.status == "open")
        .values(status="answered")
    )
    if result.rowcount == 0:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, detail="ASK_NOT_OPEN")

    answer_payload: dict = {}
    if data.choice:
        answer_payload["choice"] = data.choice
    if data.payload:
        answer_payload["extra"] = data.payload
    answer = await messages_service.append_message(
        session,
        run.id,
        role="user",
        kind="answer",
        text=text or (data.choice or ""),
        author_id=user.id,
        payload=answer_payload or None,
        reply_to=ask.id,
        step_id=ask.step_id,
    )
    await bus.publish_voyage_event(
        run.id,
        "ask.answered",
        {"message": messages_service.serialize_message(answer), "ask_id": str(ask.id)},
    )

    if data.choice == "abort":
        # 人拍板放弃：真终态。ask 直接置 consumed（不再有引擎消费）。
        ask.status = "consumed"
        run.status = "failed"
        await session.commit()
        await bus.publish_voyage_event(
            run.id, "status", {"status": run.status, "cursor": run.cursor}
        )
        if run.project_id is not None:
            await bus.publish_notify(
                run.project_id,
                {"type": "voyage.status", "voyage_id": str(run.id), "status": run.status},
            )
        experiment = await experiments_service.fail_by_voyage(session, run.id)
        if experiment is not None:
            await bus.publish_notify(
                experiment.project_id,
                {
                    "type": "experiment.status",
                    "experiment_id": str(experiment.id),
                    "status": experiment.status,
                },
            )
        return VoyageMessageRead.model_validate(answer)

    # 确定性即时效果：追加预算（缺省翻倍——比让用户拍一个数更省事）
    ask_kind = (ask.payload or {}).get("ask_kind")
    if ask_kind == "budget" and data.choice == "add_budget":
        budget = dict(run.budget or {})
        current = int(budget.get("max_tokens") or 0)
        add = int((data.payload or {}).get("add_tokens") or 0)
        used = int((run.usage or {}).get("total_tokens", 0))
        budget["max_tokens"] = current + add if add > 0 else max(current * 2, used + 10_000)
        run.budget = budget

    # 回答文本镜像成建议消息：引擎既有的注入点会把它带给下一个决策
    if text:
        await messages_service.append_message(
            session, run.id, role="user", kind="chat", text=text, author_id=user.id
        )

    # 恢复执行（条件 UPDATE：不覆盖 cancelled 等外部写入）
    await session.execute(
        sa_update(VoyageRun)
        .where(VoyageRun.id == run.id, VoyageRun.status == "paused_ask")
        .values(status="executing")
    )
    await session.commit()
    await queue.enqueue("resume_voyage", str(run.id))
    return VoyageMessageRead.model_validate(answer)


@router.post("/{voyage_id}/cancel", response_model=VoyageRead)
async def cancel_voyage(
    voyage_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> VoyageRead:
    run = await _get_owned_voyage(session, voyage_id, user)
    try:
        run = await voyages_service.cancel_voyage(session, run)
    except voyages_service.VoyageAlreadyFinishedError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="VOYAGE_ALREADY_FINISHED") from e
    return VoyageRead.model_validate(run)


@router.delete("/{voyage_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_voyage(
    voyage_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> None:
    """删除任务记录（仅限已结束的；还在跑的先取消）。

    步骤与日志一并删除；**token 用量与实验记录只解引用不删**——删任务不该把花过的
    钱从账上抹掉。
    """
    run = await _get_owned_voyage(session, voyage_id, user)
    try:
        await voyages_service.delete_voyage(session, run)
    except voyages_service.VoyageStillRunningError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="VOYAGE_STILL_RUNNING") from e


@router.post("/{voyage_id}/resume", response_model=VoyageRead)
async def resume_voyage(
    voyage_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
) -> VoyageRead:
    """重试 paused_error 的航程（如外部 API 暂时不可达），从断点续跑。

    paused_ask 不在此列：AI 在等回答，回答本身就是恢复入口（answer 端点），
    不给绕过提问的直通门。
    """
    run = await _get_owned_voyage(session, voyage_id, user)
    if run.status == "paused_ask":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="VOYAGE_WAITING_ANSWER")
    if run.status != "paused_error":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="VOYAGE_NOT_PAUSED_ERROR")
    run.status = "executing"
    await session.commit()
    await queue.enqueue("resume_voyage", str(run.id))
    return VoyageRead.model_validate(run)


@router.get("/{voyage_id}/events")
async def voyage_events(
    voyage_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    redis: Redis = Depends(get_redis_dep),
) -> StreamingResponse:
    """SSE：先补发当前状态，再订阅 redis pub/sub 转发；15s 心跳注释行。"""
    run = await _get_owned_voyage(session, voyage_id, user)
    initial = {"status": run.status, "cursor": run.cursor}

    async def stream() -> AsyncIterator[str]:
        # 先补发当前状态；终态航程不再有后续事件，直接收流
        yield _sse_frame("status", initial)
        if initial["status"] in TERMINAL_STATUSES:
            return
        pubsub = redis.pubsub()
        await pubsub.subscribe(voyage_channel(voyage_id))
        last_ping = time.monotonic()
        try:
            while True:
                message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if message is not None:
                    raw = message["data"]
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    event = str(payload.get("event", "message"))
                    data = payload.get("data")
                    yield _sse_frame(event, data)
                    # 航程进入终态 → 结束事件流
                    if (
                        event == "status"
                        and isinstance(data, dict)
                        and data.get("status") in TERMINAL_STATUSES
                    ):
                        return
                if time.monotonic() - last_ping >= _HEARTBEAT_SECONDS:
                    yield ": ping\n\n"
                    last_ping = time.monotonic()
        except asyncio.CancelledError:
            raise
        finally:
            await pubsub.unsubscribe(voyage_channel(voyage_id))
            await pubsub.aclose()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx 关缓冲
        },
    )
