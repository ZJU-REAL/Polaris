"""Voyage 路由（docs/api-m1.md §3）：创建入队 / 列表 / 详情 / 取消 / SSE 事件流。"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.events import voyage_channel
from app.core.queue import TaskQueue, get_task_queue
from app.core.redis import get_redis_dep
from app.models.user import User
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.schemas.voyage import VoyageCreate, VoyageDetailRead, VoyageRead, VoyageSkillUse
from app.services import projects as projects_service
from app.services import voyages as voyages_service

router = APIRouter(prefix="/voyages", tags=["voyages"])

_HEARTBEAT_SECONDS = 15.0


async def _get_owned_voyage(
    session: AsyncSession, voyage_id: uuid.UUID, user: User, with_steps: bool = False
) -> VoyageRun:
    run = await voyages_service.get_voyage(
        session, voyage_id=voyage_id, user_id=user.id, with_steps=with_steps
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
    return detail


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


@router.post("/{voyage_id}/resume", response_model=VoyageRead)
async def resume_voyage(
    voyage_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
) -> VoyageRead:
    """重试 paused_error 的航程（如外部 API 暂时不可达），从断点续跑。"""
    run = await _get_owned_voyage(session, voyage_id, user)
    if run.status != "paused_error":
        raise HTTPException(status.HTTP_409_CONFLICT, detail="VOYAGE_NOT_PAUSED_ERROR")
    run.status = "executing"
    await session.commit()
    await queue.enqueue("resume_voyage", str(run.id))
    return VoyageRead.model_validate(run)


def _sse_frame(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


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
