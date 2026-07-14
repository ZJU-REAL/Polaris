"""ARQ 任务。

- M1：Voyage 引擎驱动任务（run/resume）
- M2：每日文献增量 ingest（cron，见 worker/settings.py）
- M3：Idea Forge / 评审锦标赛 voyage（kind=idea_forge / idea_review，仍走 run_voyage）
未来任务归属（M4+）：
- experiment_*: 远程实验 setup/run/监控（asyncssh；写操作先过 Gate）
- writing_*: 稿件生成与编译
"""

import uuid
from typing import Any

from app.agents.voyage import VoyageEngine
from app.core.db import get_sessionmaker
from app.core.events import EventBus
from app.core.redis import get_redis
from app.schemas.ingest import IngestKnobs
from app.services import ingest as ingest_service


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
