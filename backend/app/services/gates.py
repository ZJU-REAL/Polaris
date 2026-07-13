"""闸门业务逻辑（不 import fastapi）。

Agent 流程中需要人工介入的节点创建 Gate（pending）并暂停；
人工 approve/reject 后由 worker 恢复对应任务（TODO M3：审批后通知 ARQ 恢复）。
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.gate import Gate
from app.schemas.gate import GateCreate


class GateAlreadyDecidedError(Exception):
    """重复审批已决策的闸门。"""


async def list_gates(session: AsyncSession, status: str | None = "pending") -> Sequence[Gate]:
    stmt = select(Gate).order_by(Gate.created_at.desc())
    if status:
        stmt = stmt.where(Gate.status == status)
    return (await session.execute(stmt)).scalars().all()


async def get_gate(session: AsyncSession, gate_id: uuid.UUID) -> Gate | None:
    return await session.get(Gate, gate_id)


async def create_gate(session: AsyncSession, data: GateCreate) -> Gate:
    gate = Gate(
        project_id=data.project_id,
        kind=data.kind,
        payload=data.payload,
        requested_by=data.requested_by,
    )
    session.add(gate)
    await session.commit()
    await session.refresh(gate)
    return gate


async def decide_gate(
    session: AsyncSession, gate: Gate, decided_by: uuid.UUID, approved: bool
) -> Gate:
    if gate.status != "pending":
        raise GateAlreadyDecidedError(str(gate.id))
    gate.status = "approved" if approved else "rejected"
    gate.decided_by = decided_by
    gate.decided_at = utcnow()
    await session.commit()
    await session.refresh(gate)
    # TODO(M3): 通知 ARQ worker 恢复被该闸门暂停的任务
    return gate
