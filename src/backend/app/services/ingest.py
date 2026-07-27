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
from app.models.user import User
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.schemas.ingest import IngestKnobs
from app.services.libraries import get_library_for_project

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

    任务只挂 ``library``：建库 / 增量更新是文献库自己的事，课题只是关联库来用
    语料。``project`` 仅用来取展示名（有起源课题时用课题名更好认），不写进
    run.project_id —— 写了会让库任务混进课题的任务列表，鉴权走库级写权限、
    活动流走 activity.library_id，都不需要它。
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
    run = VoyageRun(
        kind=kind,
        goal=goal,
        status="planning",
        cursor=0,
        checkpoint={"params": {"mode": mode, "knobs": knobs.model_dump()}},
        budget=budget,
        library_id=library.id,
        created_by=created_by,
    )
    session.add(run)
    session.add(
        Activity(
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


def _empty_counts() -> dict[str, int]:
    counts = {status: 0 for status in PAPER_STATUSES}
    counts["total"] = 0
    counts["library"] = 0
    counts["pending_compile"] = 0
    return counts


async def library_paper_counts(
    session: AsyncSession, library_id: uuid.UUID
) -> dict[str, int]:
    """某方向库的论文状态计数（库级直接统计 library_papers，库工作台/ingest 面板共用）。"""
    counts = {status: 0 for status in PAPER_STATUSES}
    rows = (
        await session.execute(
            select(LibraryPaper.status, func.count())
            .where(LibraryPaper.library_id == library_id)
            .group_by(LibraryPaper.status)
        )
    ).all()
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


async def paper_counts(session: AsyncSession, project_id: uuid.UUID) -> dict[str, int]:
    # P9c：课题可无起源库（空语料）——直接给零计数，不报错。
    library = await get_library_for_project(session, project_id)
    if library is None:
        return _empty_counts()
    return await library_paper_counts(session, library.id)


# 每日自动同步的触发时刻（UTC，与 worker/settings.py 的 cron 保持一致）
DAILY_SYNC_UTC_HOUR = 3
DAILY_SYNC_UTC_MINUTE = 0


def next_daily_sync_at(library: DirectionLibrary | None) -> datetime | None:
    """下一次自动同步时间：完成初始建库的库都有；未建库返回 None。

    同步节奏不再可配置——每日论文池每天更新且只保留一周，任何比「每天」更稀疏的
    节奏都意味着永久漏抓，而界面上只表现为「一直没有新论文」。
    """
    if library is None:
        return None
    state = library.ingest_state or {}
    if not state.get("watermark"):
        return None
    now = datetime.now(UTC)
    candidate = now.replace(
        hour=DAILY_SYNC_UTC_HOUR, minute=DAILY_SYNC_UTC_MINUTE, second=0, microsecond=0
    )
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


async def _can_open(session: AsyncSession, run: VoyageRun | None, user: User) -> bool:
    """这个人能否打开该任务的详情页（与 voyages 详情鉴权同一口径）。

    库能看不等于库的任务能看：只读访客看得到「正在建库」，但点不开任务详情。
    前端据此把状态渲染成纯文字而不是链接，免得点进去 404。
    """
    from app.services.voyages import can_view_voyage

    return run is not None and await can_view_voyage(session, run=run, user=user)


async def _resolve_last_run(
    session: AsyncSession, state: dict[str, Any], user: User
) -> dict[str, Any] | None:
    """从 ingest_state 里的 last_run 摘要补上当前 voyage 状态（水位线权威源在库）。"""
    last_run_raw = state.get("last_run") or None
    if not (isinstance(last_run_raw, dict) and last_run_raw.get("voyage_id")):
        return None
    voyage = await session.get(VoyageRun, uuid.UUID(str(last_run_raw["voyage_id"])))
    return {
        "voyage_id": last_run_raw["voyage_id"],
        "status": voyage.status if voyage else "unknown",
        "finished_at": last_run_raw.get("finished_at"),
        "can_open": await _can_open(session, voyage, user),
    }


async def ingest_state(
    session: AsyncSession, project: Project, *, user: User
) -> dict[str, Any]:
    # P8a：水位线/last_run 权威源在库（library.ingest_state）
    library = await get_library_for_project(session, project.id)
    state = (library.ingest_state if library else None) or {}
    # 互斥以库为准：库任务不写 project_id，按课题查已经查不到在跑的任务
    running = (
        await find_running_ingest_for_library(session, library.id) if library else None
    )
    return {
        "watermark": state.get("watermark"),
        "last_run": await _resolve_last_run(session, state, user),
        "paper_counts": await paper_counts(session, project.id),
        "running_voyage_id": running.id if running else None,
        "can_open_running_voyage": await _can_open(session, running, user),
        "next_sync_at": (next_dt.isoformat() if (next_dt := next_daily_sync_at(library)) else None),
    }


async def library_ingest_state(
    session: AsyncSession, library: DirectionLibrary, *, user: User
) -> dict[str, Any]:
    """某方向库的 ingest 状态（水位线/上次同步/计数/在跑任务/下次同步），库工作台用。

    与课题版口径一致，但一切按库直接取数：计数用 library_papers、互斥/在跑判定
    以库为准（P9a），适用于独立库（project_id=None）。
    """
    state = library.ingest_state or {}
    running = await find_running_ingest_for_library(session, library.id)
    return {
        "watermark": state.get("watermark"),
        "last_run": await _resolve_last_run(session, state, user),
        "paper_counts": await library_paper_counts(session, library.id),
        "running_voyage_id": running.id if running else None,
        "can_open_running_voyage": await _can_open(session, running, user),
        "next_sync_at": (next_dt.isoformat() if (next_dt := next_daily_sync_at(library)) else None),
    }


# paused_error 不是终态，所以一个失败的任务会被互斥判定当成「还在跑」，把该库从每日
# cron 里永久踢出去——生产上四个库就是这样静默停摆的。超过这个时长仍未被人处理的，
# 由 cron 自动取消回收；留一天窗口是给人手动 resume 的机会。
STALE_PAUSED_HOURS = 24


async def reclaim_stale_paused_ingests(session: AsyncSession) -> list[uuid.UUID]:
    """把长期无人处理的 paused_error 文献任务置为 cancelled，返回被回收的 id。

    不这样做的话，一次瞬时故障（arXiv 429、LLM 超时）就等于让这个库再也不自动同步。
    每日池只保留 7 天，那意味着永久漏抓，而界面上只表现为「一直没有新论文」。
    """
    cutoff = datetime.now(UTC) - timedelta(hours=STALE_PAUSED_HOURS)
    runs = (
        (
            await session.execute(
                select(VoyageRun).where(
                    VoyageRun.kind.in_(WIKI_KINDS),
                    VoyageRun.status == "paused_error",
                    VoyageRun.updated_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for run in runs:
        run.status = "cancelled"
    if runs:
        await session.commit()
    return [run.id for run in runs]


async def find_due_daily_libraries(session: AsyncSession) -> list[DirectionLibrary]:
    """每日增量的对象，**直接按文献库选**：active、已 bootstrap、无任务在跑。

    以前是遍历 Project 再找库，于是 ``project_id`` 为空的独立库一个都进不来——生产上
    11 个活跃库里有 6 个是独立库，全靠人工点击才会同步。另外那条路径也没检查库的
    ``status``，pending/rejected 但留有历史水位线的库照样会被 cron 拉起来。
    """
    libraries = (
        (
            await session.execute(
                select(DirectionLibrary).where(DirectionLibrary.status == "active")
            )
        )
        .scalars()
        .all()
    )
    due: list[DirectionLibrary] = []
    for library in libraries:
        state = library.ingest_state or {}
        if not state.get("watermark"):
            continue
        if await find_running_ingest_for_library(session, library.id) is not None:
            continue
        due.append(library)
    return due


