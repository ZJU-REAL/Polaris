"""闸门路由：列 pending / 审批 / 驳回（薄层，逻辑在 services/gates.py）。"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.user import User
from app.schemas.gate import GateRead
from app.services import gates as gates_service

router = APIRouter(prefix="/gates", tags=["gates"])


@router.get("", response_model=list[GateRead])
async def list_gates(
    status_filter: str | None = Query(default="pending", alias="status"),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(current_active_user),
) -> list[GateRead]:
    gates = await gates_service.list_gates(session, status=status_filter)
    return [GateRead.model_validate(g) for g in gates]


async def _decide(
    gate_id: uuid.UUID, session: AsyncSession, user: User, approved: bool
) -> GateRead:
    # TODO(M2): 权限细化（admin / 项目 owner 才能审批）
    gate = await gates_service.get_gate(session, gate_id)
    if gate is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="GATE_NOT_FOUND")
    try:
        gate = await gates_service.decide_gate(session, gate, decided_by=user.id, approved=approved)
    except gates_service.GateAlreadyDecidedError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="GATE_ALREADY_DECIDED") from e
    return GateRead.model_validate(gate)


@router.post("/{gate_id}/approve", response_model=GateRead)
async def approve_gate(
    gate_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> GateRead:
    return await _decide(gate_id, session, user, approved=True)


@router.post("/{gate_id}/reject", response_model=GateRead)
async def reject_gate(
    gate_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(current_active_user),
) -> GateRead:
    return await _decide(gate_id, session, user, approved=False)
