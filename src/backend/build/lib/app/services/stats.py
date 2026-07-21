"""项目 Dashboard 统计（docs/api-m2.md §6，不 import fastapi）。"""

import uuid
from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import Activity
from app.models.gate import Gate
from app.models.idea import Idea
from app.models.paper import Paper


async def _count(session: AsyncSession, stmt) -> int:
    return int((await session.execute(stmt)).scalar_one())


async def project_stats(session: AsyncSession, project_id: uuid.UUID) -> dict[str, Any]:
    today_start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    papers_total = await _count(session, select(func.count()).where(Paper.project_id == project_id))
    papers_today = await _count(
        session,
        select(func.count()).where(Paper.project_id == project_id, Paper.created_at >= today_start),
    )
    ideas_candidate = await _count(
        session,
        select(func.count()).where(Idea.project_id == project_id, Idea.status == "candidate"),
    )
    gates_pending = await _count(
        session,
        select(func.count()).where(Gate.project_id == project_id, Gate.status == "pending"),
    )
    activities = (
        (
            await session.execute(
                select(Activity)
                .where(Activity.project_id == project_id)
                .order_by(Activity.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    return {
        "papers_total": papers_total,
        "papers_today": papers_today,
        "ideas_candidate": ideas_candidate,
        "gates_pending": gates_pending,
        "recent_activities": [
            {"id": a.id, "kind": a.kind, "message": a.message, "created_at": a.created_at}
            for a in activities
        ],
    }
