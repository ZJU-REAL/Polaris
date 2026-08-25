"""Policy and unattended checks for managed commands waiting on a user.

The monitor which raised the question is no longer running once a voyage enters
``paused_ask``.  This service is therefore called by a worker cron job.  It is
command-name agnostic: attribution uses the durable attempt and its process
group, not Docker/pip/training command patterns.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_setting import SystemSetting
from app.models.user import User
from app.models.voyage import VoyageMessage, VoyageRun
from app.services import experiments as experiments_service
from app.services import voyage_messages as messages_service

logger = logging.getLogger(__name__)

SYSTEM_SETTING_KEY = "managed_command_watchdog"
USER_SETTING_KEY = "managed_command_unanswered_minutes"
DEFAULT_MAX_UNANSWERED_MINUTES = 120
MIN_UNANSWERED_MINUTES = 15
MAX_UNANSWERED_MINUTES = 7 * 24 * 60
RECHECK_MINUTES = 30


@dataclass(slots=True, frozen=True)
class WatchdogEvent:
    voyage_id: uuid.UUID
    project_id: uuid.UUID | None
    message: dict[str, Any]
    action: str
    used_memory_mib: int = 0


def validate_minutes(value: Any) -> int:
    try:
        minutes = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid managed-command unanswered timeout") from exc
    if not MIN_UNANSWERED_MINUTES <= minutes <= MAX_UNANSWERED_MINUTES:
        raise ValueError("managed-command unanswered timeout is out of range")
    return minutes


async def get_admin_max_minutes(session: AsyncSession) -> int:
    row = await session.get(SystemSetting, SYSTEM_SETTING_KEY)
    raw = row.value if row is not None else None
    if isinstance(raw, dict):
        try:
            return validate_minutes(raw.get("max_unanswered_minutes"))
        except ValueError:
            pass
    return DEFAULT_MAX_UNANSWERED_MINUTES


async def set_admin_max_minutes(session: AsyncSession, minutes: int) -> int:
    value = validate_minutes(minutes)
    row = await session.get(SystemSetting, SYSTEM_SETTING_KEY)
    payload = {"max_unanswered_minutes": value}
    if row is None:
        session.add(SystemSetting(key=SYSTEM_SETTING_KEY, value=payload))
    else:
        row.value = payload
    await session.commit()
    return value


def get_user_minutes(user: User, *, fallback: int) -> int:
    try:
        return validate_minutes(user.setting(USER_SETTING_KEY, fallback))
    except ValueError:
        return fallback


async def set_user_minutes(session: AsyncSession, user: User, minutes: int) -> int:
    value = validate_minutes(minutes)
    settings = dict(user.settings or {})
    settings[USER_SETTING_KEY] = value
    user.settings = settings
    await session.commit()
    await session.refresh(user)
    return value


def effective_minutes(user: User, admin_max_minutes: int) -> int:
    """The administrator caps waiting; a user may only choose an earlier stop."""
    return min(get_user_minutes(user, fallback=admin_max_minutes), admin_max_minutes)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _watchdog_context(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    context = dict(payload.get("context") or {})
    watchdog = dict(context.get("unanswered_watchdog") or {})
    return context, watchdog


def _due_for_check(
    ask: VoyageMessage,
    *,
    now: datetime,
    timeout_minutes: int,
) -> bool:
    if now - _aware(ask.created_at) < timedelta(minutes=timeout_minutes):
        return False
    payload = ask.payload if isinstance(ask.payload, dict) else {}
    _, watchdog = _watchdog_context(payload)
    raw = watchdog.get("checked_at")
    if not raw:
        return True
    try:
        checked_at = _aware(datetime.fromisoformat(str(raw)))
    except ValueError:
        return True
    return now - checked_at >= timedelta(minutes=RECHECK_MINUTES)


async def check_unanswered_managed_commands(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[WatchdogEvent]:
    """Stop only stale, still-open asks whose own command is using a GPU."""
    now = _aware(now or datetime.now(UTC))
    admin_max = await get_admin_max_minutes(session)
    rows = (
        await session.execute(
            select(VoyageMessage, VoyageRun, User)
            .join(VoyageRun, VoyageRun.id == VoyageMessage.run_id)
            .join(User, User.id == VoyageRun.created_by)
            .where(
                VoyageMessage.kind == "ask",
                VoyageMessage.status == "open",
                VoyageRun.status == "paused_ask",
            )
        )
    ).all()
    events: list[WatchdogEvent] = []
    for ask, run, user in rows:
        payload = dict(ask.payload or {})
        context, watchdog = _watchdog_context(payload)
        handle = context.get("handle")
        if payload.get("ask_kind") != "managed_command" or not isinstance(handle, dict):
            continue
        timeout = effective_minutes(user, admin_max)
        if not _due_for_check(ask, now=now, timeout_minutes=timeout):
            continue
        try:
            usage = await experiments_service.managed_command_gpu_usage_by_voyage(
                session, run.id, handle
            )
        except Exception as exc:  # noqa: BLE001 - one remote host must not block the sweep
            logger.warning("managed command watchdog probe failed for %s: %s", run.id, exc)
            status = "probe_failed"
            usage = None
        else:
            status = usage.status

        # A user may have answered while the remote probe was in flight.  Refresh
        # before non-destructive bookkeeping; destructive stop additionally uses
        # the same open -> stopping CAS as the answer endpoint below.
        await session.refresh(ask)
        await session.refresh(run)
        if ask.status != "open" or run.status != "paused_ask":
            continue

        watchdog.update(
            {
                "checked_at": now.isoformat(),
                "effective_timeout_minutes": timeout,
                "status": status,
            }
        )
        if usage is not None:
            watchdog["used_memory_mib"] = usage.used_memory_mib
            watchdog["matched_gpu_processes"] = len(usage.process_ids)

        action: str | None = None
        if usage is not None and usage.status == "active":
            claimed = await messages_service.claim_open_ask_for_stop(session, ask.id)
            if not claimed:
                continue
            try:
                stop_result = await experiments_service.stop_managed_command_by_voyage(
                    session, run.id, handle
                )
            except Exception as exc:  # noqa: BLE001 - release the durable claim
                await messages_service.release_stop_claim(session, ask.id)
                logger.warning("managed command watchdog stop failed for %s: %s", run.id, exc)
                continue
            await session.refresh(ask)
            await session.refresh(run)
            if ask.status != messages_service.STOPPING_STATUS or run.status != "paused_ask":
                # Cancellation may supersede the ask while SSH is stopping the
                # process.  The remote stop is still desirable, but the
                # watchdog must not reopen a cancelled task's question.
                continue
            stop_status = stop_result.status
            watchdog["stop_status"] = stop_status
            watchdog["status"] = (
                "command_ended" if stop_status == "already_exited" else stop_status
            )
            if stop_result:
                context["remote_operation_continues"] = False
                if stop_status == "already_exited":
                    ask.text = (
                        "等待回复期间，远端命令已自行结束。请选择继续，让 AI 读取最终状态并"
                        "诊断，或放弃任务。"
                    )
                else:
                    ask.text = (
                        f"等待回复已超过 {timeout} 分钟。系统确认当前远端命令仍占用 "
                        f"{usage.used_memory_mib} MiB GPU 显存，为避免持续占用资源已终止该命令。"
                        "请选择继续，让 AI 根据终止后的真实状态诊断并提出下一步方案，或放弃任务。"
                    )
                payload["options"] = [
                    {
                        "id": "retry",
                        "zh": "继续诊断并提出下一步方案",
                        "en": "Continue diagnosis and propose next steps",
                    },
                    {"id": "abort", "zh": "放弃任务", "en": "Abort task"},
                ]
                action = (
                    "command_ended"
                    if stop_status == "already_exited"
                    else "stopped_gpu_active"
                )
            # The stop decision is complete.  The question remains open so the
            # user can choose diagnosis/abort, but only after the remote result
            # and revised options are committed together.
            ask.status = "open"
        elif usage is not None and usage.status in {"exited", "superseded"}:
            context["remote_operation_continues"] = False
            ask.text = (
                "等待回复期间，远端命令已经结束。请选择继续，让 AI 读取最终状态并诊断，"
                "或放弃任务。"
            )
            payload["options"] = [
                {
                    "id": "retry",
                    "zh": "读取结果并继续诊断",
                    "en": "Read the result and continue diagnosis",
                },
                {"id": "abort", "zh": "放弃任务", "en": "Abort task"},
            ]
            action = "command_ended"

        context["unanswered_watchdog"] = watchdog
        payload["context"] = context
        ask.payload = payload
        await session.commit()
        await session.refresh(ask)
        if action is not None:
            events.append(
                WatchdogEvent(
                    voyage_id=run.id,
                    project_id=run.project_id,
                    message=messages_service.serialize_message(ask),
                    action=action,
                    used_memory_mib=usage.used_memory_mib if usage is not None else 0,
                )
            )
    return events
