"""文献 ingest 业务逻辑：ingest voyage 创建 / 状态查询 / 每日增量选表（不 import fastapi）。"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.navigator import WIKI_KINDS
from app.models.activity import Activity
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import PAPER_STATUSES
from app.models.project import Project
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.schemas.ingest import IngestKnobs
from app.services.libraries import get_library_for_project, library_definition

# 预算从 knobs 派生：每篇编译预留的 token 额度（打分+编译+概念定义+验证）
_TOKENS_PER_PAPER = 20_000


class IngestConflictError(Exception):
    """同一项目已有 ingest voyage 在跑。"""


class LibraryBudgetExhaustedError(Exception):
    """方向库本月预算已用尽（P6）：拒绝启动新的 ingest。"""


async def monthly_library_usage(
    session: AsyncSession, library_id: uuid.UUID
) -> dict[str, Any]:
    """该方向库本月（UTC 自然月）的 LLM 用量聚合（口径与 LLMUsage 记账一致）。"""
    from app.models.llm_config import LLMUsage  # 延迟导入避免模块环

    now = datetime.now(UTC)
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    prompt, completion = (
        await session.execute(
            select(
                func.coalesce(func.sum(LLMUsage.prompt_tokens), 0),
                func.coalesce(func.sum(LLMUsage.completion_tokens), 0),
            ).where(LLMUsage.library_id == library_id, LLMUsage.created_at >= month_start)
        )
    ).one()
    prompt, completion = int(prompt), int(completion)
    return {
        "month": now.strftime("%Y-%m"),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


async def apply_library_budget(
    session: AsyncSession,
    *,
    library_id: uuid.UUID,
    monthly_budget: int | None,
    budget: dict[str, Any],
) -> dict[str, Any]:
    """把库的月度预算折算进 run.budget（复用 Voyage 预算暂停语义，不另造状态机）。

    - 本月已用 ≥ 上限 → 抛 LibraryBudgetExhaustedError（拒绝启动）；
    - 否则 run.budget.max_tokens 收紧为 min(原值, 本月剩余)——运行中一旦累计
      到剩余额度，引擎按既有预算机制收尾/暂停。
    """
    if not monthly_budget:
        return budget
    usage = await monthly_library_usage(session, library_id)
    remaining = int(monthly_budget) - int(usage["total_tokens"])
    if remaining <= 0:
        raise LibraryBudgetExhaustedError(str(library_id))
    max_tokens = budget.get("max_tokens")
    budget = dict(budget)
    budget["max_tokens"] = remaining if not max_tokens else min(int(max_tokens), remaining)
    return budget


def derive_budget(knobs: IngestKnobs) -> dict[str, Any]:
    # 最大化模式不设 token 预算：引擎 _budget_exceeded 对 falsy 的 max_tokens（None/缺失）
    # 直接跳过预算检查（engine.py），任务不会因预算暂停/降级收尾。
    if knobs.unlimited:
        return {"max_tokens": None}
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


async def find_running_ingest_for_library(
    session: AsyncSession, library_id: uuid.UUID
) -> VoyageRun | None:
    """该方向库是否已有 ingest 任务在跑（P9a：库化后互斥以库为准）。"""
    stmt = (
        select(VoyageRun)
        .where(
            VoyageRun.library_id == library_id,
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
    library: DirectionLibrary,
    project: Project | None = None,
    mode: str,
    knobs: IngestKnobs,
    created_by: uuid.UUID | None,
) -> VoyageRun:
    """建 ingest voyage（互斥检查 + 库预算检查 + Activity 落记录），由调用方入队 run_voyage。

    P9a：任务直接挂 ``library``。有起源课题的隐式库同时带上 ``project`` 以兼容活动流/
    鉴权；管理员创建的独立库不传 project（run.project_id / activity.project_id 为空）。
    """
    if await find_running_ingest_for_library(session, library.id) is not None:
        raise IngestConflictError(str(library.id))
    budget = await apply_library_budget(
        session,
        library_id=library.id,
        monthly_budget=library.monthly_budget,
        budget=derive_budget(knobs),
    )
    kind = "wiki_bootstrap" if mode == "bootstrap" else "wiki_ingest"
    target_name = project.name if project is not None else library.name
    goal = (
        f"文献调研初始建库：{target_name}"
        if mode == "bootstrap"
        else f"文献调研增量更新：{target_name}"
    )
    project_id = project.id if project is not None else None
    run = VoyageRun(
        kind=kind,
        goal=goal,
        status="planning",
        cursor=0,
        checkpoint={"params": {"mode": mode, "knobs": knobs.model_dump()}},
        budget=budget,
        project_id=project_id,
        library_id=library.id,
        created_by=created_by,
    )
    session.add(run)
    session.add(
        Activity(
            project_id=project_id,
            library_id=library.id,
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
    library = await get_library_for_project(session, project_id)
    rows = (
        await session.execute(
            select(LibraryPaper.status, func.count())
            .where(LibraryPaper.library_id == library.id)
            .group_by(LibraryPaper.status)
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


def next_daily_sync_at(library: DirectionLibrary | None) -> datetime | None:
    """下一次自动同步时间：cadence=daily 且已完成初始建库才有；否则 None。

    P8a：节奏/水位线权威源在库（library.definition.cadence / library.ingest_state）。
    """
    from app.services.libraries import library_definition

    if library is None:
        return None
    definition = library_definition(library)
    state = library.ingest_state or {}
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
    # P8a：水位线/last_run 权威源在库（library.ingest_state）
    library = await get_library_for_project(session, project.id)
    state = (library.ingest_state if library else None) or {}
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
        "next_sync_at": (next_dt.isoformat() if (next_dt := next_daily_sync_at(library)) else None),
    }


async def find_due_daily_projects(session: AsyncSession) -> list[Project]:
    """每日增量对象：active、cadence=daily、已 bootstrap（有水位线）、无 ingest 在跑。"""
    projects = (
        (await session.execute(select(Project).where(Project.status == "active"))).scalars().all()
    )
    due: list[Project] = []
    for project in projects:
        # P8a：节奏/水位线读起源库（project.definition 不再是权威源）
        library = await get_library_for_project(session, project.id)
        if library is None:
            continue
        definition = library_definition(library)
        state = library.ingest_state or {}
        if definition.get("cadence") != "daily" or not state.get("watermark"):
            continue
        if await find_running_ingest(session, project.id) is not None:
            continue
        due.append(project)
    return due
