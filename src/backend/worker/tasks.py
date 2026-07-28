"""ARQ 任务。

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


def _make_engine() -> VoyageEngine:
    return VoyageEngine(event_bus=EventBus(get_redis()))


async def run_voyage(ctx: dict[str, Any], run_id: str) -> None:
    """驱动一次新航程（POST /voyages 入队）。"""
    await _make_engine().run(uuid.UUID(run_id))


async def resume_voyage(ctx: dict[str, Any], run_id: str) -> None:
    """闸门批准后从断点恢复航程（gates approve 入队）。"""
    await _make_engine().resume(uuid.UUID(run_id))


async def reconcile_stuck_voyages(ctx: dict[str, Any]) -> None:
    """worker 启动对账：认领无人执行的 executing 航程。

    被 SIGTERM/超时打断的 ARQ 任务按任务年龄指数延迟重试，长航程会被晾数小时
    （实测：远端 run.sh 已 exit=0，平台侧 50 分钟无人收尾）。启动时把 executing
    状态的 voyage 重新入队 resume——引擎幂等（setup/run 都会重挂在跑的远端进程，
    checkpoint 断点恢复），``_job_id`` 去重避免同一 voyage 重复入队。"""
    from sqlalchemy import select

    from app.models.voyage import VoyageRun

    async with get_sessionmaker()() as session:
        ids = (
            (await session.execute(select(VoyageRun.id).where(VoyageRun.status == "executing")))
            .scalars()
            .all()
        )
    for vid in ids:
        await ctx["redis"].enqueue_job("resume_voyage", str(vid), _job_id=f"reconcile-resume-{vid}")


async def daily_wiki_ingest(ctx: dict[str, Any]) -> list[str]:
    """给已建库的**文献库**入队同步。由每日论文抓取跑完后触发（daily.sync_libraries）。

    不再自己定时：定时就意味着赌抓取已经跑完，抓取慢一点或失败重试，同步就会在旧池子
    上空跑一整轮，而界面上只显示「0 篇新论文」。每天只跑一轮（同一天重复触发会被挡）。

    按库选而不是按课题选：独立库（project_id 为空）在按课题遍历的老写法里一个都进不来。

    每个库单独兜异常。以前只捕获 IngestConflictError，于是一个库超月度预算抛
    LibraryBudgetExhaustedError 就会打断整个循环，**排在它后面的库当天全都不同步**，
    而且只在 arq 日志里留个异常，界面上完全无感。

    返回本次入队的 voyage id 列表（arq 结果可查）。
    """
    import datetime as dt

    from app.services import daily_feed as daily_feed_service

    now = dt.datetime.now(dt.UTC)
    enqueued: list[str] = []
    async with get_sessionmaker()() as session:
        if await daily_feed_service.already_ran_today(session, "wiki_ingest", now=now):
            return enqueued
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
            except ingest_service.LibraryBudgetExhaustedError:
                logger.warning("daily ingest skipped, budget exhausted: %s", library.id)
                continue
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
        fresh, batch_date = await daily_feed_service.todays_batch_available(session)
        if not fresh:
            logger.info("daily feed not published yet (latest batch %s), will retry", batch_date)
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
