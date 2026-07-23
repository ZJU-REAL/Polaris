"""项目 Dashboard 统计（docs/api-m2.md §6，不 import fastapi）。"""

import uuid
from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import Activity
from app.models.experiment import EXPERIMENT_TERMINAL_STATUSES, Experiment
from app.models.gate import Gate
from app.models.idea import Idea
from app.models.library_direction import LibraryPaper
from app.models.manuscript import Manuscript
from app.services.libraries import get_source_library_ids
from app.services.papers import PAPER_STATUS_GROUPS


async def _count(session: AsyncSession, stmt) -> int:
    return int((await session.execute(stmt)).scalar_one())


async def project_stats(session: AsyncSession, project_id: uuid.UUID) -> dict[str, Any]:
    today_start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    # 「知识库论文」与文献页口径一致：只算库内（相关性达标及之后），
    # 不含回收站（excluded）与尚未打分的中间状态（candidate）
    # 「知识库论文」= 关联库并集内（相关性达标及之后），跨库同一论文只计一次
    library_ids = await get_source_library_ids(session, project_id)
    in_library = LibraryPaper.status.in_(PAPER_STATUS_GROUPS["library"])
    if library_ids:
        papers_total = await _count(
            session,
            select(func.count(func.distinct(LibraryPaper.paper_id))).where(
                LibraryPaper.library_id.in_(library_ids), in_library
            ),
        )
        papers_today = await _count(
            session,
            select(func.count(func.distinct(LibraryPaper.paper_id))).where(
                LibraryPaper.library_id.in_(library_ids),
                in_library,
                LibraryPaper.created_at >= today_start,
            ),
        )
    else:
        papers_total = 0
        papers_today = 0
    ideas_candidate = await _count(
        session,
        select(func.count()).where(Idea.project_id == project_id, Idea.status == "candidate"),
    )
    ideas_under_review = await _count(
        session,
        select(func.count()).where(Idea.project_id == project_id, Idea.status == "under_review"),
    )
    experiments_active = await _count(
        session,
        select(func.count()).where(
            Experiment.project_id == project_id,
            Experiment.status.notin_(EXPERIMENT_TERMINAL_STATUSES),
        ),
    )
    experiments_running = await _count(
        session,
        select(func.count()).where(
            Experiment.project_id == project_id, Experiment.status == "running"
        ),
    )
    manuscripts_total = await _count(
        session, select(func.count()).where(Manuscript.project_id == project_id)
    )
    manuscripts_under_review = await _count(
        session,
        select(func.count()).where(
            Manuscript.project_id == project_id, Manuscript.status == "under_review"
        ),
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
        "ideas_under_review": ideas_under_review,
        "experiments_active": experiments_active,
        "experiments_running": experiments_running,
        "manuscripts_total": manuscripts_total,
        "manuscripts_under_review": manuscripts_under_review,
        "gates_pending": gates_pending,
        "recent_activities": [
            {"id": a.id, "kind": a.kind, "message": a.message, "created_at": a.created_at}
            for a in activities
        ],
    }
