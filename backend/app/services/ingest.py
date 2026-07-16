"""文献 ingest 业务逻辑：ingest voyage 创建 / 状态查询 / 每日增量选表（不 import fastapi）。"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.navigator import WIKI_KINDS
from app.models.activity import Activity
from app.models.paper import PAPER_STATUSES, Paper
from app.models.project import Project
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.schemas.ingest import IngestKnobs

# 预算从 knobs 派生：每篇编译预留的 token 额度（打分+编译+概念定义+验证）
_TOKENS_PER_PAPER = 20_000


class IngestConflictError(Exception):
    """同一项目已有 ingest voyage 在跑。"""


def derive_budget(knobs: IngestKnobs) -> dict[str, Any]:
    return {"max_tokens": int(knobs.max_papers) * _TOKENS_PER_PAPER}


async def find_running_ingest(session: AsyncSession, project_id: uuid.UUID) -> VoyageRun | None:
    stmt = (
        select(VoyageRun)
        .where(
            VoyageRun.project_id == project_id,
            VoyageRun.kind.in_(WIKI_KINDS),
            VoyageRun.status.not_in(tuple(TERMINAL_STATUSES)),
        )
        .order_by(VoyageRun.created_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_ingest_voyage(
    session: AsyncSession,
    *,
    project: Project,
    mode: str,
    knobs: IngestKnobs,
    created_by: uuid.UUID | None,
) -> VoyageRun:
    """建 ingest voyage（互斥检查 + Activity 落记录），由调用方入队 run_voyage。"""
    if await find_running_ingest(session, project.id) is not None:
        raise IngestConflictError(str(project.id))
    kind = "wiki_bootstrap" if mode == "bootstrap" else "wiki_ingest"
    goal = (
        f"文献调研初始建库：{project.name}（回填 {knobs.months_back} 个月，"
        f"编译上限 {knobs.max_papers} 篇）"
        if mode == "bootstrap"
        else f"文献调研增量更新：{project.name}（从上次同步时间续跑）"
    )
    run = VoyageRun(
        kind=kind,
        goal=goal,
        status="planning",
        cursor=0,
        checkpoint={"params": {"mode": mode, "knobs": knobs.model_dump()}},
        budget=derive_budget(knobs),
        project_id=project.id,
        created_by=created_by,
    )
    session.add(run)
    session.add(
        Activity(
            project_id=project.id,
            actor=f"user:{created_by}" if created_by else "system:cron",
            kind="ingest.started",
            message=f"文献调研{'初始建库' if mode == 'bootstrap' else '增量更新'}已启动",
            payload={"mode": mode, "knobs": knobs.model_dump()},
        )
    )
    await session.commit()
    await session.refresh(run)
    return run


async def paper_counts(session: AsyncSession, project_id: uuid.UUID) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Paper.status, func.count())
            .where(Paper.project_id == project_id)
            .group_by(Paper.status)
        )
    ).all()
    counts = {status: 0 for status in PAPER_STATUSES}
    total = 0
    for status, count in rows:
        counts[status] = int(count)
        total += int(count)
    counts["total"] = total
    # 库内 = 相关性达标及之后（论文库默认视图/计数口径，docs/api-lit.md §8.5）
    counts["library"] = (
        counts["scored"] + counts["fetched"] + counts["compiled"] + counts["included"]
    )
    counts["pending_compile"] = counts["scored"] + counts["fetched"]
    return counts


# 每日自动同步的触发时刻（UTC，与 worker/settings.py 的 cron 保持一致）
DAILY_SYNC_UTC_HOUR = 3
DAILY_SYNC_UTC_MINUTE = 0


def next_daily_sync_at(project: Project) -> datetime | None:
    """下一次自动同步时间：cadence=daily 且已完成初始建库才有；否则 None。"""
    definition = project.definition if isinstance(project.definition, dict) else {}
    state = project.ingest_state or {}
    if definition.get("cadence") != "daily" or not state.get("watermark"):
        return None
    now = datetime.now(UTC)
    candidate = now.replace(
        hour=DAILY_SYNC_UTC_HOUR, minute=DAILY_SYNC_UTC_MINUTE, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


async def ingest_state(session: AsyncSession, project: Project) -> dict[str, Any]:
    state = project.ingest_state or {}
    last_run_raw = state.get("last_run") or None
    last_run: dict[str, Any] | None = None
    if isinstance(last_run_raw, dict) and last_run_raw.get("voyage_id"):
        voyage = await session.get(VoyageRun, uuid.UUID(str(last_run_raw["voyage_id"])))
        last_run = {
            "voyage_id": last_run_raw["voyage_id"],
            "status": voyage.status if voyage else "unknown",
            "finished_at": last_run_raw.get("finished_at"),
        }
    running = await find_running_ingest(session, project.id)
    return {
        "watermark": state.get("watermark"),
        "last_run": last_run,
        "paper_counts": await paper_counts(session, project.id),
        "running_voyage_id": running.id if running else None,
        "next_sync_at": (next_dt.isoformat() if (next_dt := next_daily_sync_at(project)) else None),
    }


async def find_due_daily_projects(session: AsyncSession) -> list[Project]:
    """每日增量对象：active、cadence=daily、已 bootstrap（有水位线）、无 ingest 在跑。"""
    projects = (
        (await session.execute(select(Project).where(Project.status == "active"))).scalars().all()
    )
    due: list[Project] = []
    for project in projects:
        definition = project.definition or {}
        state = project.ingest_state or {}
        if definition.get("cadence") != "daily" or not state.get("watermark"):
            continue
        if await find_running_ingest(session, project.id) is not None:
            continue
        due.append(project)
    return due
