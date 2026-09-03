"""Persist and dispatch library-scoped incremental discovery schedules."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.library_direction import DirectionLibrary
from app.models.literature_discovery import (
    LiteratureDiscoverySchedule,
    LiteratureSearchRun,
)
from app.schemas.literature_discovery import (
    LiteratureDiscoveryScheduleUpdate,
    LiteratureSearchRequest,
)
from app.services import libraries as libraries_service
from app.services.literature.discovery_runs import create_discovery_run

ACTIVE_RUN_STATUSES = ("queued", "running")
SCHEDULE_TRIGGER = "scheduled"


class InvalidDiscoveryScheduleError(ValueError):
    """A stable schedule-validation error suitable for an API response."""


class DiscoveryScheduleRunConflictError(RuntimeError):
    """The library already has an active scheduled discovery run."""


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def schedule_timezone(name: str) -> ZoneInfo:
    normalized = name.strip()
    try:
        return ZoneInfo(normalized)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise InvalidDiscoveryScheduleError("INVALID_TIMEZONE") from exc


def next_occurrence(
    *, timezone: str, hour: int, minute: int, after: datetime
) -> datetime:
    """Return the next wall-clock occurrence as an aware UTC timestamp."""

    zone = schedule_timezone(timezone)
    local_after = _aware(after).astimezone(zone)
    candidate = datetime.combine(
        date=local_after.date(),
        time=time(hour=hour, minute=minute),
        tzinfo=zone,
    )
    if candidate <= local_after:
        candidate = datetime.combine(
            date=local_after.date() + timedelta(days=1),
            time=time(hour=hour, minute=minute),
            tzinfo=zone,
        )
    return candidate.astimezone(UTC)


async def get_schedule(
    session: AsyncSession, library_id: uuid.UUID
) -> LiteratureDiscoverySchedule | None:
    return await session.get(LiteratureDiscoverySchedule, library_id)


async def _serialize_first_write(session: AsyncSession, library_id: uuid.UUID) -> None:
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:library_id))"),
            {"library_id": str(library_id)},
        )


async def upsert_schedule(
    session: AsyncSession,
    *,
    library: DirectionLibrary,
    data: LiteratureDiscoveryScheduleUpdate,
    actor_id: uuid.UUID,
    now: datetime | None = None,
) -> LiteratureDiscoverySchedule:
    await _serialize_first_write(session, library.id)
    schedule = await session.get(
        LiteratureDiscoverySchedule, library.id, with_for_update=True
    )
    values = data.model_dump()
    schedule_timezone(values["timezone"])
    current = _aware(now or datetime.now(UTC))
    if schedule is None:
        schedule = LiteratureDiscoverySchedule(
            library_id=library.id,
            created_by=actor_id,
            config_version=1,
            **values,
        )
        session.add(schedule)
    else:
        for key, value in values.items():
            setattr(schedule, key, value)
        schedule.config_version += 1
    schedule.next_run_at = (
        next_occurrence(
            timezone=schedule.timezone,
            hour=schedule.hour,
            minute=schedule.minute,
            after=current,
        )
        if schedule.enabled
        else None
    )
    schedule.last_error_code = None
    await session.commit()
    await session.refresh(schedule)
    return schedule


async def delete_schedule(
    session: AsyncSession, schedule: LiteratureDiscoverySchedule
) -> None:
    await session.delete(schedule)
    await session.commit()


def effective_topic(library: DirectionLibrary) -> str:
    definition = libraries_service.library_definition(library)
    for value in (definition.get("statement"), library.statement, library.name):
        text_value = " ".join(str(value or "").split())
        if text_value:
            return text_value[:4000]
    raise InvalidDiscoveryScheduleError("LIBRARY_TOPIC_MISSING")


async def create_scheduled_run(
    session: AsyncSession,
    *,
    library: DirectionLibrary,
    schedule: LiteratureDiscoverySchedule,
    created_by: uuid.UUID | None,
    scheduled_for: datetime,
) -> LiteratureSearchRun:
    request = LiteratureSearchRequest(
        topic=effective_topic(library),
        requested_count=schedule.requested_count,
        candidate_budget=schedule.candidate_budget,
        start_year=schedule.start_year,
        end_year=schedule.end_year,
        source_config={
            "incremental_discovery": {
                "schedule_version": schedule.config_version,
                "scheduled_for": _aware(scheduled_for).isoformat(),
            }
        },
    )
    return await create_discovery_run(
        session,
        library=library,
        data=request,
        created_by=created_by,
        trigger=SCHEDULE_TRIGGER,
        schedule_version=schedule.config_version,
        scheduled_for=_aware(scheduled_for),
    )


async def trigger_schedule_now(
    session: AsyncSession,
    *,
    library: DirectionLibrary,
    schedule: LiteratureDiscoverySchedule,
    actor_id: uuid.UUID,
    now: datetime | None = None,
) -> LiteratureSearchRun:
    current = _aware(now or datetime.now(UTC))
    locked_schedule = await session.scalar(
        select(LiteratureDiscoverySchedule)
        .where(LiteratureDiscoverySchedule.library_id == schedule.library_id)
        .with_for_update()
    )
    if locked_schedule is not None:
        schedule = locked_schedule
    if await _active_scheduled_run(session, library.id) is not None:
        raise DiscoveryScheduleRunConflictError(str(library.id))
    try:
        async with session.begin_nested():
            run = await create_scheduled_run(
                session,
                library=library,
                schedule=schedule,
                created_by=actor_id,
                scheduled_for=current,
            )
    except IntegrityError as exc:
        raise DiscoveryScheduleRunConflictError(str(library.id)) from exc
    schedule.last_run_id = run.id
    schedule.last_enqueued_at = None
    schedule.last_error_code = None
    await session.commit()
    await session.refresh(run)
    return run


async def _active_scheduled_run(
    session: AsyncSession, library_id: uuid.UUID
) -> LiteratureSearchRun | None:
    return await session.scalar(
        select(LiteratureSearchRun)
        .where(
            LiteratureSearchRun.library_id == library_id,
            LiteratureSearchRun.trigger == SCHEDULE_TRIGGER,
            LiteratureSearchRun.status.in_(ACTIVE_RUN_STATUSES),
        )
        .order_by(LiteratureSearchRun.created_at.desc())
        .limit(1)
    )


async def claim_due_schedules(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 50,
) -> list[uuid.UUID]:
    """Atomically create due runs and return queued run IDs for ARQ dispatch."""

    current = _aware(now or datetime.now(UTC))
    statement = (
        select(LiteratureDiscoverySchedule)
        .where(
            LiteratureDiscoverySchedule.enabled.is_(True),
            LiteratureDiscoverySchedule.next_run_at.is_not(None),
            LiteratureDiscoverySchedule.next_run_at <= current,
        )
        .order_by(LiteratureDiscoverySchedule.next_run_at)
        .limit(limit)
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    schedules = list((await session.execute(statement)).scalars().all())
    run_ids: list[uuid.UUID] = []
    for schedule in schedules:
        scheduled_for = _aware(schedule.next_run_at or current)
        schedule.next_run_at = next_occurrence(
            timezone=schedule.timezone,
            hour=schedule.hour,
            minute=schedule.minute,
            after=current,
        )
        active = await _active_scheduled_run(session, schedule.library_id)
        if active is not None:
            schedule.last_run_id = active.id
            if active.status == "queued":
                run_ids.append(active.id)
            else:
                schedule.last_error_code = "RUN_ALREADY_ACTIVE"
            continue
        library = await session.get(DirectionLibrary, schedule.library_id)
        if library is None:
            # 库已被删而定时任务行还挂着（审批流移除后这是唯一的「库不可用」情形）
            schedule.last_error_code = "LIBRARY_NOT_FOUND"
            continue
        try:
            async with session.begin_nested():
                run = await create_scheduled_run(
                    session,
                    library=library,
                    schedule=schedule,
                    created_by=None,
                    scheduled_for=scheduled_for,
                )
        except Exception as exc:
            schedule.last_error_code = type(exc).__name__.upper()[:64]
            continue
        schedule.last_run_id = run.id
        schedule.last_enqueued_at = None
        schedule.last_error_code = None
        run_ids.append(run.id)

    retry_cutoff = current - timedelta(minutes=15)
    retry_ids = list(
        (
            await session.execute(
                select(LiteratureSearchRun.id)
                .join(
                    LiteratureDiscoverySchedule,
                    LiteratureDiscoverySchedule.last_run_id == LiteratureSearchRun.id,
                )
                .where(
                    LiteratureDiscoverySchedule.enabled.is_(True),
                    LiteratureSearchRun.trigger == SCHEDULE_TRIGGER,
                    LiteratureSearchRun.status == "queued",
                    (
                        LiteratureDiscoverySchedule.last_enqueued_at.is_(None)
                        | (LiteratureDiscoverySchedule.last_enqueued_at < retry_cutoff)
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    await session.commit()
    return list(dict.fromkeys([*run_ids, *retry_ids]))


async def record_dispatch_result(
    session: AsyncSession,
    *,
    run_id: uuid.UUID,
    ok: bool,
    now: datetime | None = None,
) -> None:
    schedule = await session.scalar(
        select(LiteratureDiscoverySchedule).where(
            LiteratureDiscoverySchedule.last_run_id == run_id
        )
    )
    if schedule is None:
        return
    schedule.last_enqueued_at = _aware(now or datetime.now(UTC)) if ok else None
    schedule.last_error_code = None if ok else "QUEUE_DISPATCH_FAILED"
    await session.commit()
