"""后台任务：由进程内任务队列（app/core/queue.py）在引擎进程里作为 asyncio 任务执行。

- M1：Voyage 引擎驱动任务（run/resume）
- M2：每日文献增量 ingest（cron，见 worker/settings.py）
- M3：Idea Forge / 评审锦标赛 voyage（kind=idea_forge / idea_review，仍走 run_voyage）
- M4：Experiment Lab voyage（kind=experiment，仍走 run_voyage；SSH 执行与轮询
  在 actions_experiment 内部，命令白名单见 app/services/ssh_exec.py）
- M5-B：论文撰写 voyage（kind=paper_writing，仍走 run_voyage；分节撰写/静态校验/
  tectonic 编译在 actions_writing + services/latex_compile 内部）
- M5-C：论文评审 voyage（kind=paper_review，仍走 run_voyage；引用核验/事实查错/
  评审员×3/聚合在 actions_review + services/paper_review 内部）
"""

import logging
import uuid
from typing import Any

from app.agents.voyage import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.events import EventBus
from app.core.redis import get_redis
from app.models.project import Project
from app.schemas.ingest import IngestKnobs
from app.services import ingest as ingest_service
from app.services import publications as publications_service

logger = logging.getLogger(__name__)


async def ping_task(ctx: dict[str, Any], message: str = "ping") -> str:
    """连通性验证用示例任务。"""
    return f"pong: {message}"


async def parse_paper_content_task(
    ctx: dict[str, Any], version_id: str, user_id: str | None = None, library_id: str | None = None
) -> None:
    """Parse one immutable content version; MinerU adapters can be injected later."""
    from app.models.paper_content import PaperContentVersion
    from app.services.paper_content import parse_content_version, vectorize_content_version

    async with get_sessionmaker()() as session:
        version = await session.get(PaperContentVersion, uuid.UUID(version_id))
        if version is None:
            return
        await parse_content_version(session, version=version)
        try:
            await vectorize_content_version(
                session,
                version=version,
                user_id=uuid.UUID(user_id) if user_id else None,
                library_id=uuid.UUID(library_id) if library_id else None,
            )
        except Exception:
            logger.exception("content vectorization failed for %s", version_id)


def _make_engine() -> VoyageEngine:
    return VoyageEngine(event_bus=EventBus(get_redis()))


async def run_voyage(ctx: dict[str, Any], run_id: str) -> None:
    """驱动一次新航程（POST /voyages 入队）。"""
    await _make_engine().run(uuid.UUID(run_id))


async def resume_voyage(ctx: dict[str, Any], run_id: str) -> None:
    """闸门批准后从断点恢复航程（gates approve 入队）。"""
    await _make_engine().resume(uuid.UUID(run_id))


RECONCILE_STALE_MINUTES = 30  # 周期回收：终端无动静超过这个时长才算僵死


async def _enqueue_reclaim(ctx: dict[str, Any], vid: object) -> bool:
    """把一条无人驱动的在途航程重新排上 resume；已有驱动者在跑（或在排队）就跳过。

    任务 id 与 API 入队的是同一个（voyage_job_id）：内联队列的去重因此能认出来，
    ``_if_idle`` 再保证撞上在途驱动者时直接丢弃，而不是排成它收尾后的续跑——
    以前对账用按时间分桶的 id，跑着的长实验每 15 分钟会被多塞一个驱动者（#850）。"""
    from app.core.queue import voyage_job_id

    job_id = voyage_job_id(vid)
    is_running = getattr(ctx["redis"], "is_running", None)
    if is_running is not None and is_running(job_id):
        return False
    await ctx["redis"].enqueue_job("resume_voyage", str(vid), _job_id=job_id, _if_idle=True)
    return True


async def reconcile_stuck_voyages(ctx: dict[str, Any]) -> None:
    """启动对账：认领无人执行的在途航程（见 IN_FLIGHT_STATUSES）。

    进程被杀时在跑的航程停在在途状态、再没人推进。启动时把它们重新入队 resume——
    引擎幂等（setup/run 都会重挂在跑的远端进程，checkpoint 断点恢复）。"""
    from sqlalchemy import select

    from app.models.voyage import IN_FLIGHT_STATUSES, VoyageRun

    async with get_sessionmaker()() as session:
        ids = (
            (
                await session.execute(
                    select(VoyageRun.id).where(VoyageRun.status.in_(tuple(IN_FLIGHT_STATUSES)))
                )
            )
            .scalars()
            .all()
        )
    for vid in ids:
        await _enqueue_reclaim(ctx, vid)


async def reconcile_stale_voyages(
    ctx: dict[str, Any], stale_minutes: int = RECONCILE_STALE_MINUTES
) -> None:
    """周期回收（cron）：在途但终端长时间无动静的 voyage 重新入队 resume。

    启动对账只救重启这一种孤儿；驱动任务在运行中途意外退出、却没把航程落到终态或
    暂停态产生的僵死靠这里兜底。判据有二，缺一不可：

    - 本进程里没有这条航程的驱动任务（在跑或排队等名额）——这是真正的存活判据。
      长实验轮询几个小时不写终端日志是常态，光看日志会把活着的航程判死、再塞一个
      驱动者进去（#850）；
    - 距最后一条终端日志（无日志则取创建时间）超过 ``stale_minutes``：给刚入队、
      刚收尾的航程留出余量。"""
    from datetime import timedelta

    from sqlalchemy import func as sa_func
    from sqlalchemy import or_, select

    from app.models.base import utcnow
    from app.models.voyage import IN_FLIGHT_STATUSES, VoyageRun, VoyageTerminalLog

    cutoff = utcnow() - timedelta(minutes=stale_minutes)
    last_log = (
        select(
            VoyageTerminalLog.run_id.label("run_id"),
            sa_func.max(VoyageTerminalLog.at).label("last_at"),
        )
        .group_by(VoyageTerminalLog.run_id)
        .subquery()
    )
    async with get_sessionmaker()() as session:
        ids = (
            (
                await session.execute(
                    select(VoyageRun.id)
                    .outerjoin(last_log, last_log.c.run_id == VoyageRun.id)
                    .where(
                        VoyageRun.status.in_(tuple(IN_FLIGHT_STATUSES)),
                        or_(
                            last_log.c.last_at < cutoff,
                            (last_log.c.last_at.is_(None)) & (VoyageRun.created_at < cutoff),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    if not ids:
        return
    reclaimed = [vid for vid in ids if await _enqueue_reclaim(ctx, vid)]
    if reclaimed:
        logger.warning("reclaiming %d stale in-flight voyage(s): %s", len(reclaimed), reclaimed)


async def watch_unanswered_managed_commands(ctx: dict[str, Any]) -> int:
    """Enforce the unattended GPU wait policy for open managed-command asks."""
    from app.core.events import EventBus
    from app.services.managed_command_watchdog import check_unanswered_managed_commands

    async with get_sessionmaker()() as session:
        events = await check_unanswered_managed_commands(session)
    bus = EventBus(ctx["redis"])
    for event in events:
        await bus.publish_voyage_event(
            event.voyage_id,
            "ask.updated",
            {"message": event.message, "action": event.action},
        )
        if event.user_id is not None:
            await bus.publish_notify(
                event.user_id,
                {
                    "type": "voyage.ask.updated",
                    "voyage_id": str(event.voyage_id),
                    "action": event.action,
                    "used_memory_mib": event.used_memory_mib,
                },
            )
    return len(events)


async def daily_wiki_ingest(ctx: dict[str, Any]) -> list[str]:
    """给已建库的**文献库**入队同步。由每日论文抓取跑完后触发（daily.sync_libraries）。

    不再自己定时：定时就意味着赌抓取已经跑完，抓取慢一点或失败重试，同步就会在旧池子
    上空跑一整轮，而界面上只显示「0 篇新论文」。每天只跑一轮（同一天重复触发会被挡）。

    按库选而不是按课题选：独立库（project_id 为空）在按课题遍历的老写法里一个都进不来。

    每个库单独兜异常：一个库启动失败不能打断整个循环，否则排在它后面的库当天
    全都不同步，而且只在 arq 日志里留个异常，界面上完全无感（月度预算硬限额
    时代实测发生过；硬限额已随 #734 移除，这条纪律保留防其他异常复现同样的坑）。

    返回本次入队的 voyage id 列表（arq 结果可查）。
    """
    enqueued: list[str] = []
    async with get_sessionmaker()() as session:
        # 「今天只跑一轮」的判据在 find_due_daily_libraries 里，且是**按库**算的。
        # 这里以前是一道全局闸门（今天有任意一条 wiki_ingest 就整轮返回），于是
        # 任何人手动同步任何一个库，当天其余所有库的自动同步全部被吞掉。
        # 先回收长期卡住的 paused_error：它们会把所在库一直挡在互斥判定外面
        reclaimed = await ingest_service.reclaim_stale_paused_ingests(session)
        if reclaimed:
            logger.warning("reclaimed %d stale paused ingest run(s)", len(reclaimed))

        libraries = await ingest_service.find_due_daily_libraries(session)
        for library in libraries:
            project = (
                await session.get(Project, library.project_id)
                if library.project_id is not None
                else None
            )
            try:
                run = await ingest_service.create_ingest_voyage(
                    session,
                    library=library,
                    project=project,
                    mode="incremental",
                    knobs=IngestKnobs(),
                    created_by=None,
                )
            except ingest_service.IngestConflictError:
                continue  # 并发保护：查表与建 run 之间有人手动触发
            except Exception:  # noqa: BLE001 — 单个库出问题不能拖垮当天其余的库
                logger.exception("daily ingest failed to start for library %s", library.id)
                await session.rollback()
                continue
            await ctx["redis"].enqueue_job("run_voyage", str(run.id))
            enqueued.append(str(run.id))
    return enqueued


async def match_user_publications(ctx: dict[str, Any], user_id: str) -> int:
    """扫描文献库为某用户匹配发表候选（姓名+机构命中 → pending）；返回新增数。"""
    async with get_sessionmaker()() as session:
        return await publications_service.match_from_library(session, user_id=uuid.UUID(user_id))


async def index_papers_fulltext_task(
    ctx: dict[str, Any], scope: str, user_id: str, project_id: str | None = None
) -> dict[str, Any]:
    """可选全文索引：按 scope 解析论文集合，批量抓 PDF→分段→嵌入（文献对话检索底座）。

    scope=="shelf" → 课题相关研究书架论文（需 project_id）；
    scope=="personal" → 本人收藏的个人库论文。
    """
    from app.core.llm.router import get_llm_router
    from app.services.fulltext_index import index_papers_fulltext
    from app.services.topic_shelf import shelf_paper_ids
    from app.services.user_library import personal_paper_ids

    uid = uuid.UUID(user_id)
    async with get_sessionmaker()() as session:
        if scope == "shelf":
            if project_id is None:
                raise ValueError("shelf scope requires project_id")
            paper_ids = await shelf_paper_ids(session, project_id=uuid.UUID(project_id))
        elif scope == "personal":
            paper_ids = await personal_paper_ids(session, user_id=uid, tab="saved")
        else:
            raise ValueError(f"unknown scope: {scope}")
        return await index_papers_fulltext(
            session, paper_ids=paper_ids, llm=get_llm_router(), user_id=uid
        )


async def daily_feed_sync(ctx: dict[str, Any]) -> str | None:
    """检查点任务（每 15 分钟一次）：**探到今天那批公告出来了**才抓。

    起始时刻可配置（默认 UTC 01:30 = 北京 09:30），从那时起每 15 分钟探一次，直到
    arXiv 放出当天批次。不定死"几点抓"是因为发布时刻会飘，赌一个固定点赌输的表现
    是拿到上一批、去重后一条不进、每一步却都报成功。

    先探再建任务，不是建了任务让它失败——否则从早到晚会攒一堆 paused_error。

    arq 的 cron 时刻在 worker 启动时固定，改设置得重启才生效，所以让 cron 空转、
    由设置决定是否动手。空转一次只是一条查询加一次 RSS 探测。

    返回入队的 voyage id；未到点 / 今天已跑过 / 今天那批还没出来，都返回 None。
    """
    import datetime as dt

    from app.services import daily_feed as daily_feed_service

    now = dt.datetime.now(dt.UTC)
    async with get_sessionmaker()() as session:
        if not await daily_feed_service.due_now(session, now=now):
            return None
        if await daily_feed_service.already_ran_today(
            session, daily_feed_service.DAILY_FEED_VOYAGE_KIND, now=now
        ):
            return None
        # 探测有次数上限：「今天 arXiv 就是没发」是正常情况（周末、节假日、发布故障），
        # 不该从早探到晚每 15 分钟敲一次。但**探满不等于当天收工**：/new 只带当天那批，
        # 今天的公告错过了就永久没有了，所以探满之后转成一小时一次的复查。
        max_attempts = await daily_feed_service.get_max_probe_attempts(session)
        state = await daily_feed_service.probe_state(session, now=now)
        if not daily_feed_service.should_probe_now(state, now=now, max_attempts=max_attempts):
            return None
        fresh, batch_date = await daily_feed_service.todays_batch_available(session)
        if not fresh:
            state = await daily_feed_service.record_probe(
                session,
                now=now,
                batch_date=batch_date,
                exhausted=state["attempts"] + 1 >= max_attempts,
            )
            if state["exhausted"]:
                logger.info(
                    "daily feed probe exhausted after %s attempts (latest batch %s); "
                    "no new announcement today",
                    state["attempts"],
                    batch_date,
                )
            else:
                logger.info(
                    "daily feed not published yet (latest batch %s), probe %s/%s",
                    batch_date,
                    state["attempts"],
                    max_attempts,
                )
            return None
        try:
            run = await daily_feed_service.create_daily_feed_voyage(session, created_by=None)
        except daily_feed_service.DailyFeedConflictError:
            return None  # 并发保护：查表与建 run 之间有 admin 手动触发
    await ctx["redis"].enqueue_job("run_voyage", str(run.id))
    return str(run.id)


#: 发表匹配排在库同步之后多久（新入库论文要先落库，匹配才有东西可匹）
_PUBLICATION_MATCH_DELAY_MINUTES = 150


async def daily_publication_match(ctx: dict[str, Any]) -> int:
    """检查点任务：库同步之后再跑发表匹配（新入库论文 → 姓名+机构命中进待确认）。

    时刻同样跟随每日池抓取时间派生，不写死。
    """
    import datetime as dt

    from app.services import daily_feed as daily_feed_service

    now = dt.datetime.now(dt.UTC)
    async with get_sessionmaker()() as session:
        if not await daily_feed_service.due_now(
            session, now=now, delay_minutes=_PUBLICATION_MATCH_DELAY_MINUTES
        ):
            return 0
        if not await daily_feed_service.claim_today(
            session, "daily_publication_match_last_run", now=now
        ):
            return 0
        user_ids = await publications_service.profiles_for_daily_match(session)
    total = 0
    for uid in user_ids:
        async with get_sessionmaker()() as session:
            total += await publications_service.match_from_library(session, user_id=uid)
    return total


async def run_literature_discovery(ctx: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Execute one persisted library literature-discovery run."""
    from app.services.literature.runtime import run_discovery

    async with get_sessionmaker()() as session:
        run = await run_discovery(session, uuid.UUID(run_id))
        return {
            "run_id": str(run.id),
            "status": run.status,
            "returned_count": (run.progress or {}).get("returned_count", 0),
        }


async def dispatch_literature_discovery_schedules(ctx: dict[str, Any]) -> list[str]:
    """Create due incremental runs and idempotently dispatch them to ARQ."""

    from datetime import UTC, datetime

    from app.services.literature import discovery_schedules

    current = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        run_ids = await discovery_schedules.claim_due_schedules(session, now=current)
    dispatched: list[str] = []
    bucket = int(current.timestamp() // (15 * 60))
    for run_id in run_ids:
        ok = False
        try:
            await ctx["redis"].enqueue_job(
                "run_literature_discovery",
                str(run_id),
                _job_id=f"scheduled-literature-{run_id}-{bucket}",
            )
            ok = True
            dispatched.append(str(run_id))
        except Exception:
            logger.exception("scheduled literature dispatch failed for %s", run_id)
        async with get_sessionmaker()() as session:
            await discovery_schedules.record_dispatch_result(
                session,
                run_id=run_id,
                ok=ok,
                now=current,
            )
    return dispatched


async def translate_literature_hit(ctx: dict[str, Any], translation_id: str) -> dict[str, Any]:
    """Translate one discovery hit through the dedicated versioned LLM route."""

    from sqlalchemy import select

    from app.core.llm.router import get_llm_router
    from app.models.literature_discovery import (
        LiteratureHitTranslation,
        LiteratureSearchHit,
        LiteratureSearchRun,
    )
    from app.services.literature.translations import execute_translation

    del ctx
    async with get_sessionmaker()() as session:
        identity = await session.execute(
            select(LiteratureHitTranslation.requested_by, LiteratureSearchRun.library_id)
            .join(LiteratureSearchHit, LiteratureSearchHit.run_id == LiteratureSearchRun.id)
            .join(
                LiteratureHitTranslation,
                LiteratureHitTranslation.hit_id == LiteratureSearchHit.id,
            )
            .where(LiteratureHitTranslation.id == uuid.UUID(translation_id))
        )
        owner = identity.one_or_none()
        row = await execute_translation(
            session,
            translation_id=uuid.UUID(translation_id),
            llm=get_llm_router(),
            user_id=owner.requested_by if owner else None,
            library_id=owner.library_id if owner else None,
        )
        return {
            "translation_id": translation_id,
            "status": row.status if row is not None else "missing",
        }


async def zotero_import(
    ctx: dict[str, Any],
    *,
    task_id: str,
    bib_path: str,
    zip_path: str | None,
    library_id: str,
    user_id: str,
    project_id: str | None = None,
) -> dict[str, int]:
    """Zotero 库导入（#638）：API 侧把上传暂存到 data_dir 后入队，这里逐条落库。

    文件走共享数据卷（api 与 worker 同挂 /srv/data），进度与结果按 paper-task
    事件口径发（任务归属在 API 入队前已注册），前端订阅
    /paper-tasks/{task_id}/events。返回汇总计数（arq 结果可查）。
    """
    from app.services.zotero_import import run_zotero_import

    return await run_zotero_import(
        ctx["redis"],
        task_id=task_id,
        bib_path=bib_path,
        zip_path=zip_path or None,
        library_id=uuid.UUID(library_id),
        user_id=uuid.UUID(user_id),
        project_id=uuid.UUID(project_id) if project_id else None,
    )


async def full_export(ctx: dict[str, Any], *, task_id: str, user_id: str) -> dict[str, Any]:
    """一键全量导出（#690）：把用户全部数据面打包成 zip 供下载。

    zip 落共享数据卷 data_dir/exports/<task_id>.zip（api 侧下载端点直接读），
    进度与完成事件走 paper-task 通道（归属在 API 入队前已注册），并发锁
    （每用户同时一个）由任务结束时释放。
    """
    from app.services.full_export import run_full_export_task

    return await run_full_export_task(ctx["redis"], task_id=task_id, user_id=user_id)
