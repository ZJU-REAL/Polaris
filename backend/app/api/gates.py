"""闸门路由（docs/api-m1.md §4）：列表 / 审批 / 驳回。

- 项目成员可见与可审批本项目闸门（M2 再细化角色权限）；
- approve：payload.voyage_id 存在时入队 resume_voyage 恢复航程；
  payload.idea_id 存在时联动 idea.status=promoted 并发布 idea.status 事件（M3）；
- reject：关联航程置 failed；
- 决策后向 ``notify:project:{project_id}`` 发布 gate.decided。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.core.events import EventBus, get_event_bus
from app.core.queue import TaskQueue, get_task_queue
from app.models.gate import Gate
from app.models.user import User
from app.schemas.gate import GateDecision, GateRead
from app.services import experiments as experiments_service
from app.services import gates as gates_service
from app.services import ideas as ideas_service

router = APIRouter(prefix="/gates", tags=["gates"])


@router.get("", response_model=list[GateRead])
async def list_gates(
    status_filter: str | None = Query(default="pending", alias="status"),
    project_id: uuid.UUID | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> list[GateRead]:
    gates = await gates_service.list_gates(
        session, user_id=user.id, status=status_filter, project_id=project_id
    )
    return [GateRead.model_validate(g) for g in gates]


async def _get_decidable_gate(session: AsyncSession, gate_id: uuid.UUID, user: User) -> Gate:
    """成员才可见/可审批；非成员一律 404（不泄露存在性）。"""
    gate = await gates_service.get_gate(session, gate_id)
    if gate is None or not await gates_service.is_project_member(session, gate.project_id, user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="GATE_NOT_FOUND")
    return gate


async def _decide(
    *,
    gate_id: uuid.UUID,
    session: AsyncSession,
    user: User,
    approved: bool,
    data: GateDecision | None,
    queue: TaskQueue,
    bus: EventBus,
) -> GateRead:
    gate = await _get_decidable_gate(session, gate_id, user)
    try:
        gate = await gates_service.decide_gate(
            session,
            gate,
            decided_by=user.id,
            approved=approved,
            comment=data.comment if data else None,
        )
    except gates_service.GateAlreadyDecidedError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="GATE_ALREADY_DECIDED") from e

    gate_read = GateRead.model_validate(gate)

    # 关联航程联动：批准 → 入队恢复；驳回 → 置 failed
    voyage_id = gates_service.gate_voyage_id(gate)
    if voyage_id is not None:
        if approved:
            await queue.enqueue("resume_voyage", str(voyage_id))
        else:
            run = await gates_service.fail_voyage(session, voyage_id)
            if run is not None:
                await bus.publish_voyage_event(
                    voyage_id, "status", {"status": run.status, "cursor": run.cursor}
                )
                await bus.publish_notify(
                    run.project_id,
                    {
                        "type": "voyage.status",
                        "voyage_id": str(voyage_id),
                        "status": run.status,
                    },
                )
            # 实验联动（M4）：驳回 compute_budget 闸门 → 关联实验置 failed + WS 事件
            experiment = await experiments_service.fail_by_voyage(session, voyage_id)
            if experiment is not None:
                await bus.publish_notify(
                    experiment.project_id,
                    {
                        "type": "experiment.status",
                        "experiment_id": str(experiment.id),
                        "status": experiment.status,
                    },
                )

    # idea 晋级联动（M3）：批准 idea_promotion 闸门 → idea.status=promoted + WS 事件
    if approved:
        idea = await ideas_service.promote_from_gate(session, gate)
        if idea is not None:
            await bus.publish_notify(
                gate.project_id,
                {"type": "idea.status", "idea_id": str(idea.id), "status": idea.status},
            )

    await bus.publish_notify(
        gate.project_id,
        {"type": "gate.decided", "gate": gate_read.model_dump(mode="json")},
    )
    return gate_read


@router.post("/{gate_id}/approve", response_model=GateRead)
async def approve_gate(
    gate_id: uuid.UUID,
    data: GateDecision | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
    bus: EventBus = Depends(get_event_bus),
) -> GateRead:
    return await _decide(
        gate_id=gate_id,
        session=session,
        user=user,
        approved=True,
        data=data,
        queue=queue,
        bus=bus,
    )


@router.post("/{gate_id}/reject", response_model=GateRead)
async def reject_gate(
    gate_id: uuid.UUID,
    data: GateDecision | None = None,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
    queue: TaskQueue = Depends(get_task_queue),
    bus: EventBus = Depends(get_event_bus),
) -> GateRead:
    return await _decide(
        gate_id=gate_id,
        session=session,
        user=user,
        approved=False,
        data=data,
        queue=queue,
        bus=bus,
    )
