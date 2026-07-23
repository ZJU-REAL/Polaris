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

import uuid
from typing import Any

from app.agents.voyage import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.events import EventBus
from app.core.redis import get_redis
from app.schemas.ingest import IngestKnobs
from app.services import ingest as ingest_service
from app.services import publications as publications_service


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
    """每日 03:00 cron：对 cadence=daily 且已 bootstrap（有水位线）的项目入队增量 ingest。

    返回本次入队的 voyage id 列表（arq 结果可查）。
    """
    enqueued: list[str] = []
    async with get_sessionmaker()() as session:
        projects = await ingest_service.find_due_daily_projects(session)
        for project in projects:
            try:
                run = await ingest_service.create_ingest_voyage(
                    session,
                    project=project,
                    mode="incremental",
                    knobs=IngestKnobs(),
                    created_by=None,
                )
            except ingest_service.IngestConflictError:
                continue  # 并发保护：查表与建 run 之间有人手动触发
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


async def daily_publication_match(ctx: dict[str, Any]) -> int:
    """每日 04:00 cron（每日 ingest 之后）：对开了自动匹配的绑定用户逐个跑库内匹配。"""
    async with get_sessionmaker()() as session:
        user_ids = await publications_service.profiles_for_daily_match(session)
    total = 0
    for uid in user_ids:
        async with get_sessionmaker()() as session:
            total += await publications_service.match_from_library(session, user_id=uid)
    return total
