"""闸门业务逻辑（不 import fastapi）。

Agent 流程中需要人工介入的节点创建 Gate（pending）并暂停；
人工 approve/reject 后由 api 层入队 resume_voyage 恢复对应航程。
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.gate import Gate
from app.models.project import Project
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.schemas.gate import GateCreate
from app.services.projects import in_my_projects


class GateAlreadyDecidedError(Exception):
    """重复审批已决策的闸门。"""


async def list_gates(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    status: str | None = "pending",
    project_id: uuid.UUID | None = None,
) -> Sequence[Gate]:
    """列出用户所在项目的闸门（平台管理员看全部）。

    status: pending | decided（=approved/rejected）。
    """
    stmt = (
        select(Gate)
        .where(in_my_projects(Gate.project_id, user_id))
        .order_by(Gate.created_at.desc())
    )
    if status == "decided":
        stmt = stmt.where(Gate.status.in_(["approved", "rejected"]))
    elif status:
        stmt = stmt.where(Gate.status == status)
    if project_id is not None:
        stmt = stmt.where(Gate.project_id == project_id)
    return (await session.execute(stmt)).scalars().all()


async def get_gate(session: AsyncSession, gate_id: uuid.UUID) -> Gate | None:
    return await session.get(Gate, gate_id)


async def can_access_project(
    session: AsyncSession, project_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """能否在这个课题里操作：课题成员，或平台管理员（最高权限）。

    审批闸门、跑技能、开报告都用它。原来只认成员身份，管理员在自己没参与的
    课题里会被挡——与「管理员看得到这些课题和它们的任务」自相矛盾。
    """
    stmt = select(Project.id).where(
        Project.id == project_id, in_my_projects(Project.id, user_id)
    )
    return (await session.execute(stmt)).first() is not None


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
    session: AsyncSession,
    gate: Gate,
    *,
    decided_by: uuid.UUID,
    approved: bool,
    comment: str | None = None,
) -> Gate:
    if gate.status != "pending":
        raise GateAlreadyDecidedError(str(gate.id))
    gate.status = "approved" if approved else "rejected"
    gate.decided_by = decided_by
    gate.decided_at = utcnow()
    gate.comment = comment
    await session.commit()
    await session.refresh(gate)
    return gate


def gate_voyage_id(gate: Gate) -> uuid.UUID | None:
    """从 payload 提取关联的 voyage_id（无/非法则 None）。"""
    raw = (gate.payload or {}).get("voyage_id")
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


async def fail_voyage(session: AsyncSession, voyage_id: uuid.UUID) -> VoyageRun | None:
    """闸门驳回时把关联航程置为 failed（终态航程不动）。"""
    run = await session.get(VoyageRun, voyage_id)
    if run is None or run.status in TERMINAL_STATUSES:
        return run
    run.status = "failed"
    await session.commit()
    await session.refresh(run)
    return run
