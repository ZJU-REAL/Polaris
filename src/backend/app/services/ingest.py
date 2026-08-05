"""文献 ingest 业务逻辑：ingest voyage 创建 / 状态查询 / 每日增量选表（不 import fastapi）。"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.voyage.navigator import WIKI_KINDS
from app.models.activity import Activity
from app.models.library_direction import DirectionLibrary, LibraryPaper
from app.models.paper import PAPER_STATUSES, Paper
from app.models.project import Project
from app.models.research_digest import LibraryResearchDigest
from app.models.user import User
from app.models.voyage import TERMINAL_STATUSES, VoyageRun
from app.schemas.ingest import IngestKnobs
from app.services.libraries import get_library_for_project

# 预算从 knobs 派生：每篇编译预留的 token 额度（打分+编译+概念定义+验证）
_TOKENS_PER_PAPER = 20_000


#: 三种收集模式。search/snowball 是人主动发起的两条补充路径，incremental 是每天自动
#: 从每日论文池挑。``bootstrap`` 是 ``search`` 的旧名，只做入口兼容，不再往下传。
MODE_LABELS: dict[str, str] = {
    "search": "按查询词检索入库",
    "snowball": "从锚点论文扩展",
    "incremental": "每日自动同步",
}


def normalize_mode(mode: str) -> str:
    """把存量的 ``bootstrap`` 折算成 ``search``；未知值一律按 ``search`` 处理。"""
    if mode == "bootstrap":
        return "search"
    return mode if mode in MODE_LABELS else "search"


class IngestConflictError(Exception):
    """同一项目已有 ingest voyage 在跑。"""


class LibraryBudgetExhaustedError(Exception):
    """方向库本月预算已用尽（P6）：拒绝启动新的 ingest。"""


async def monthly_library_usage(session: AsyncSession, library_id: uuid.UUID) -> dict[str, Any]:
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
    query_terms: list[str] | None = None,
    time_range: str | None = None,
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
    mode = normalize_mode(mode)
    # 手动的两种模式（search/snowball）沿用 wiki_bootstrap 这个 kind：它们都是"人主动去
    # 拉一批"，与自动的每日同步区分开即可；kind 是存量数据的形状，不为了新名字去迁移。
    kind = "wiki_ingest" if mode == "incremental" else "wiki_bootstrap"
    target_name = project.name if project is not None else library.name
    goal = f"{MODE_LABELS[mode]}：{target_name}"
    run = VoyageRun(
        kind=kind,
        goal=goal,
        status="planning",
        cursor=0,
        checkpoint={
            "params": {
                "mode": mode,
                "knobs": knobs.model_dump(),
                "query_terms": query_terms or None,
                "time_range": time_range,
            }
        },
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
            message=f"{MODE_LABELS[mode]}已启动",
            payload={"mode": mode, "knobs": knobs.model_dump()},
        )
    )
    await session.commit()
    await session.refresh(run)
    return run


async def create_digest_voyage(
    session: AsyncSession,
    *,
    library: DirectionLibrary,
    project: Project | None = None,
    created_by: uuid.UUID | None,
    knobs: IngestKnobs | None = None,
) -> tuple[VoyageRun, str, int]:
    """智能启动今日简报。

    UTC 当天已有完成相关性判断的论文时，只建「简报 + 趋势」两步任务并复用这些
    论文；当天没有论文更新时，退回正常增量 ingest。简报专用任务不包含水位线步骤，
    因为它没有访问任何外部数据源。
    """
    if await find_running_ingest_for_library(session, library.id) is not None:
        raise IngestConflictError(str(library.id))

    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = (
        await session.execute(
            select(LibraryPaper.paper_id, LibraryPaper.status, Paper.published_at)
            .join(Paper, Paper.id == LibraryPaper.paper_id)
            .where(
                LibraryPaper.library_id == library.id,
                LibraryPaper.scored_at.is_not(None),
                # 只有下界没有上界。此前这里还有 `scored_at <= now`，防的是"未来的打分
                # 时间"——而那恰恰只有时钟跳变才制造得出来：墙钟在打分之后回拨一秒，
                # now 就小于刚打的 scored_at，论文掉出窗口，paper_count 少一截，策略从
                # digest_only 翻成 incremental（#234 一族的偶发）。合法数据里 scored_at
                # 不会在未来，这个上界什么都不保护，只会翻车。
                LibraryPaper.scored_at >= day_start,
            )
            .order_by(LibraryPaper.scored_at, LibraryPaper.paper_id)
        )
    ).all()
    if not rows:
        run = await create_ingest_voyage(
            session,
            library=library,
            project=project,
            mode="incremental",
            knobs=knobs or IngestKnobs(),
            created_by=created_by,
        )
        return run, "incremental", 0

    actual_knobs = knobs or IngestKnobs()
    budget = await apply_library_budget(
        session,
        library_id=library.id,
        monthly_budget=library.monthly_budget,
        budget=derive_budget(actual_knobs),
    )
    paper_ids = list(dict.fromkeys(row.paper_id for row in rows))
    latest_published_at = max(
        (row.published_at for row in rows if row.published_at is not None), default=None
    )

    # 当天已有原生简报时保留其来源统计；本次只重新综合内容，不伪装成又抓了一遍来源。
    prior_digest = (
        await session.execute(
            select(LibraryResearchDigest).where(
                LibraryResearchDigest.library_id == library.id,
                LibraryResearchDigest.report_date == now.date(),
                LibraryResearchDigest.source == "voyage",
            )
        )
    ).scalar_one_or_none()
    digest_counts = (
        dict(prior_digest.counts or {})
        if prior_digest is not None
        else {
            "source_fetched": 0,
            "prescreened": len(paper_ids),
            "inserted": 0,
            "compiled": sum(1 for row in rows if row.status in {"compiled", "included"}),
        }
    )
    diagnostics = dict(prior_digest.source_diagnostics or {}) if prior_digest is not None else {}
    prior_messages = diagnostics.get("messages")
    messages = list(prior_messages) if isinstance(prior_messages, list) else []
    messages.append(
        f"检测到今日已有 {len(paper_ids)} 篇论文完成更新；"
        "本次跳过增量抓取，直接生成简报与滚动趋势。"
    )
    diagnostics.update(
        {
            "status": diagnostics.get("status") or "ok",
            "messages": messages,
            "source_latest_at": diagnostics.get("source_latest_at")
            or (latest_published_at.isoformat() if latest_published_at else None),
        }
    )

    target_name = project.name if project is not None else library.name
    checkpoint = {
        "params": {
            "mode": "incremental",
            "knobs": actual_knobs.model_dump(),
            "digest_only": True,
        },
        "digest_paper_ids": [str(paper_id) for paper_id in paper_ids],
        "digest_counts": digest_counts,
        "digest_excluded_papers": list(prior_digest.excluded_papers or [])
        if prior_digest is not None
        else [],
        "ingest_search_stats": diagnostics,
    }
    run = VoyageRun(
        kind="wiki_ingest",
        goal=f"生成今日文献简报：{target_name}",
        status="planning",
        cursor=0,
        checkpoint=checkpoint,
        budget=budget,
        library_id=library.id,
        created_by=created_by,
    )
    session.add(run)
    session.add(
        Activity(
            library_id=library.id,
            actor=f"user:{created_by}" if created_by else "system",
            kind="digest.started",
            message=f"今日简报生成已启动（复用 {len(paper_ids)} 篇今日更新论文）",
            payload={"strategy": "digest_only", "paper_count": len(paper_ids)},
        )
    )
    await session.commit()
    await session.refresh(run)
    return run, "digest_only", len(paper_ids)


def _empty_counts() -> dict[str, int]:
    counts = {status: 0 for status in PAPER_STATUSES}
    counts["total"] = 0
    counts["library"] = 0
    counts["pending_compile"] = 0
    return counts


async def library_paper_counts(session: AsyncSession, library_id: uuid.UUID) -> dict[str, int]:
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
#: 读不到每日抓取时刻时的兜底（与 daily_feed.DEFAULT_SYNC_UTC 一致）。
DAILY_SYNC_UTC_HOUR = 1
DAILY_SYNC_UTC_MINUTE = 30


def next_daily_sync_at(
    library: DirectionLibrary | None, *, fetch_at: tuple[int, int] | None = None
) -> datetime | None:
    """下一次自动同步的**大致**时刻：完成初始建库的库都有；未建库返回 None。

    库同步是事件驱动的——每日论文抓完、且真有新论文入池才触发（见
    ``actions_daily.sync_libraries``）。所以这里给的是「抓取时刻」，同步紧随其后。
    以前这里写死 03:00 UTC，而抓取默认 01:30 UTC：界面报 11:00（北京），实际约 09:35
    就跑完了，差着一个半小时。孤立地看这只是个小数字，但它属于「界面说的和实际发生的
    不是一回事」那一类——用户照着它等，等到的是已经结束的东西。

    ``fetch_at`` 由调用方从设置里读（本函数不碰 session）；不给就用默认时刻。
    真正的触发还取决于 arXiv 当天几点发布，所以这是估计值，不是承诺。

    同步节奏不可配置：每日论文池每天更新且只保留一周，任何比「每天」更稀疏的节奏
    都意味着永久漏抓，而界面上只表现为「一直没有新论文」。
    """
    if library is None:
        return None
    state = library.ingest_state or {}
    if not state.get("watermark"):
        return None
    hour, minute = fetch_at or (DAILY_SYNC_UTC_HOUR, DAILY_SYNC_UTC_MINUTE)
    now = datetime.now(UTC)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
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


async def ingest_state(session: AsyncSession, project: Project, *, user: User) -> dict[str, Any]:
    # P8a：水位线/last_run 权威源在库（library.ingest_state）
    library = await get_library_for_project(session, project.id)
    state = (library.ingest_state if library else None) or {}
    # 互斥以库为准：库任务不写 project_id，按课题查已经查不到在跑的任务
    running = await find_running_ingest_for_library(session, library.id) if library else None
    return {
        "watermark": state.get("watermark"),
        "last_run": await _resolve_last_run(session, state, user),
        "paper_counts": await paper_counts(session, project.id),
        "running_voyage_id": running.id if running else None,
        "can_open_running_voyage": await _can_open(session, running, user),
        "next_sync_at": (
            next_dt.isoformat()
            if (next_dt := next_daily_sync_at(library, fetch_at=await _daily_fetch_at(session)))
            else None
        ),
    }


async def _daily_fetch_at(session: AsyncSession) -> tuple[int, int]:
    """每日论文的抓取时刻（库同步紧随其后）。延迟 import 避开循环依赖。"""
    from app.services import daily_feed

    return await daily_feed.get_sync_time(session)


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
        "next_sync_at": (
            next_dt.isoformat()
            if (next_dt := next_daily_sync_at(library, fetch_at=await _daily_fetch_at(session)))
            else None
        ),
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
        (await session.execute(select(DirectionLibrary).where(DirectionLibrary.status == "active")))
        .scalars()
        .all()
    )
    # 今天已经**自动**同步过的库不再重复选。这一条以前写在调用方，而且是全局的：
    # 「今天有任意一条 wiki_ingest」就整轮跳过。于是任何人手动同步任何一个库，当天
    # 其余所有库的自动同步全部消失——生产上 07-31 就是这样，10:22 有人手动同步了
    # rubric，10:32 每日池刚灌进 227 篇新论文，扇出却一个库都没选。
    # 手动运行不计入：它跑在池子更新之前，不能替代这一轮。只认**跑成功**的——失败或
    # 被回收（cancelled）的那次什么也没同步，把它算成「今天已经跑过」就等于让一次
    # 瞬时故障吃掉这个库当天的同步。在跑的那些由下面的互斥判定各自挡住。
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    auto_today = set(
        (
            await session.execute(
                select(VoyageRun.library_id).where(
                    VoyageRun.kind == "wiki_ingest",
                    VoyageRun.created_by.is_(None),
                    VoyageRun.status == "done",
                    VoyageRun.created_at >= start,
                )
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
        if library.id in auto_today:
            continue
        if await find_running_ingest_for_library(session, library.id) is not None:
            continue
        due.append(library)
    return due
