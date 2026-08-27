"""库作用域文献发现运行的持久化查询和权限规则。"""

import uuid
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import DirectionLibrary, DirectionLibraryCurator
from app.models.literature_discovery import (
    LiteratureSearchHit,
    LiteratureSearchRun,
)
from app.models.user import User


async def can_manage_discovery(
    session: AsyncSession, *, library: DirectionLibrary, user: User
) -> bool:
    """发现运行写权限：平台管理员、库创建者或策展人。"""
    if user.role == "admin" or library.submitted_by == user.id:
        return True
    return (
        await session.scalar(
            select(DirectionLibraryCurator.user_id).where(
                DirectionLibraryCurator.library_id == library.id,
                DirectionLibraryCurator.user_id == user.id,
            )
        )
        is not None
    )


def enabled_sources(source_config: dict | None, query_plan: dict | None) -> list[str]:
    """从已保存快照中取得稳定来源顺序；没有配置时不凭空创建来源任务。"""
    sources: Iterable[str] = ()
    sources_declared = isinstance(source_config, dict) and "sources" in source_config
    if isinstance(source_config, dict):
        sources = source_config["sources"] if "sources" in source_config else source_config.keys()
    if not list(sources) and not sources_declared and isinstance(query_plan, dict):
        sources = query_plan.get("sources") or ()
    return list(dict.fromkeys(str(s).strip().lower() for s in sources if str(s).strip()))


async def get_visible_run(
    session: AsyncSession, *, library_id: uuid.UUID, run_id: uuid.UUID
) -> LiteratureSearchRun | None:
    return await session.scalar(
        select(LiteratureSearchRun).where(
            LiteratureSearchRun.id == run_id,
            LiteratureSearchRun.library_id == library_id,
        )
    )


async def delete_run(session: AsyncSession, run: LiteratureSearchRun) -> None:
    await session.execute(delete(LiteratureSearchRun).where(LiteratureSearchRun.id == run.id))


def score_value(hit: LiteratureSearchHit, key: str) -> float:
    scores = hit.scores if isinstance(hit.scores, dict) else {}
    value = scores.get(key)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
