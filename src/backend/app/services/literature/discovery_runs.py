"""库作用域文献发现运行的持久化查询和权限规则。"""

import uuid
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import DirectionLibrary
from app.models.literature_discovery import (
    LiteratureSearchHit,
    LiteratureSearchRun,
    LiteratureSourceAttempt,
)
from app.models.user import User
from app.schemas.literature_discovery import LiteratureSearchRequest
from app.services import libraries as libraries_service
from app.services import literature_settings as literature_settings_service
from app.services.interdisciplinary_retrieval import apply_profile_to_query_plan
from app.services.literature.discovery_ranking import normalized_score_weights


async def can_manage_discovery(
    session: AsyncSession, *, library: DirectionLibrary, user: User
) -> bool:
    """发现运行写权限：平台管理员或库创建者。"""
    return user.role == "admin" or library.submitted_by == user.id


def enabled_sources(source_config: dict | None, query_plan: dict | None) -> list[str]:
    """从已保存快照中取得稳定来源顺序；没有配置时不凭空创建来源任务。"""
    sources: Iterable[str] = ()
    sources_declared = isinstance(source_config, dict) and "sources" in source_config
    if isinstance(source_config, dict):
        sources = source_config["sources"] if "sources" in source_config else source_config.keys()
    if not list(sources) and not sources_declared and isinstance(query_plan, dict):
        sources = query_plan.get("sources") or ()
    return list(dict.fromkeys(str(s).strip().lower() for s in sources if str(s).strip()))


def library_discovery_config(library: DirectionLibrary) -> dict[str, object]:
    """Project the library's authoritative inclusion rules into one run snapshot."""

    definition = libraries_service.library_definition(library)
    raw_keywords = definition.get("keywords")
    keyword_config = raw_keywords if isinstance(raw_keywords, dict) else {}
    include = [
        str(item).strip() for item in keyword_config.get("include") or [] if str(item).strip()
    ]
    exclude = [
        str(item).strip() for item in keyword_config.get("exclude") or [] if str(item).strip()
    ]
    output: dict[str, object] = {}
    if include:
        output["keywords"] = list(dict.fromkeys(include))
    if exclude:
        output["excluded_keywords"] = list(dict.fromkeys(exclude))
    if definition.get("rubric"):
        output["score_rubric"] = definition["rubric"]
    return output


async def create_discovery_run(
    session: AsyncSession,
    *,
    library: DirectionLibrary,
    data: LiteratureSearchRequest,
    created_by: uuid.UUID | None,
    trigger: str = "manual",
    schedule_version: int | None = None,
    scheduled_for: datetime | None = None,
) -> LiteratureSearchRun:
    """Create one run from the same settings and library contract for every trigger."""

    defaults = await literature_settings_service.get_runtime_settings(session)
    requested_count = data.requested_count or int(defaults["requested_count"])
    candidate_budget = max(
        requested_count,
        data.candidate_budget or int(defaults["candidate_budget"]),
    )
    start_year = data.start_year if data.start_year is not None else defaults.get("start_year")
    end_year = data.end_year if data.end_year is not None else defaults.get("end_year")
    source_config = {
        "sources": list(defaults["sources"]),
        "score_weights": dict(defaults["score_weights"]),
        **library_discovery_config(library),
        **(data.source_config or {}),
    }
    source_config["score_weights"] = normalized_score_weights(
        source_config.get("score_weights")
        if isinstance(source_config.get("score_weights"), dict)
        else None
    )
    source_config.pop("provider_keys", None)
    query_plan = await apply_profile_to_query_plan(
        session,
        library=library,
        topic=data.topic,
        query_plan=data.query_plan,
        source_config=source_config,
    )
    run = LiteratureSearchRun(
        library_id=library.id,
        created_by=created_by,
        requested_count=requested_count,
        candidate_budget=candidate_budget,
        start_year=start_year,
        end_year=end_year,
        topic=data.topic,
        query_plan=query_plan,
        source_config=source_config,
        model_version=data.model_version,
        trigger=trigger,
        schedule_version=schedule_version,
        scheduled_for=scheduled_for,
        progress={
            "phase": "queued",
            "fetched": 0,
            "accepted": 0,
            "requested_count": requested_count,
            "candidate_budget": candidate_budget,
            "returned_count": 0,
            "start_year": start_year,
            "end_year": end_year,
            "trigger": trigger,
            "schedule_version": schedule_version,
        },
    )
    session.add(run)
    await session.flush()
    for source in enabled_sources(source_config, query_plan):
        session.add(
            LiteratureSourceAttempt(
                run_id=run.id,
                source=source,
                status="pending",
                requested_count=None,
            )
        )
    await session.flush()
    return run


async def list_source_attempts(
    session: AsyncSession, run_id: uuid.UUID
) -> list[LiteratureSourceAttempt]:
    return list(
        (
            await session.execute(
                select(LiteratureSourceAttempt)
                .where(LiteratureSourceAttempt.run_id == run_id)
                .order_by(LiteratureSourceAttempt.source)
            )
        )
        .scalars()
        .all()
    )


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
