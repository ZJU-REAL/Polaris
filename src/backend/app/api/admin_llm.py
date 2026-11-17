"""模型用量与调用日志。

模型本身不可配置（Polaris 只用 Claude，见 api/llm_status.py），这里只剩两样只读的
东西：按天汇总的用量，以及可开关的调用日志。
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_active_user
from app.core.db import get_session
from app.models.llm_config import LLMCallLog
from app.models.user import User
from app.schemas.llm_admin import (
    CallLogDetail,
    CallLogPage,
    CallLogRow,
    CallLogSettings,
    UsageRow,
)
from app.services import llm_admin as llm_admin_service

router = APIRouter(prefix="/admin/llm", tags=["admin-llm"])


@router.get("/usage", response_model=list[UsageRow])
async def usage_report(
    _user: User = Depends(current_active_user),
    project_id: uuid.UUID | None = Query(default=None),
    user_id: uuid.UUID | None = Query(default=None),
    days: int = Query(default=30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
) -> list[UsageRow]:
    rows = await llm_admin_service.usage_report(
        session, project_id=project_id, user_id=user_id, days=days
    )
    return [UsageRow(**row) for row in rows]


# ---- 调用日志 ----

_PREVIEW_CHARS = 200


def _preview(text: str, limit: int = _PREVIEW_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _request_preview(request: object) -> str:
    """取最后一条消息内容的截断预览（embed/rerank 摘要取首条输入）。"""
    if not isinstance(request, dict):
        return ""
    messages = request.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            return _preview(str(last.get("content", "")))
    for key in ("first_text", "query"):
        if key in request:
            return _preview(str(request.get(key) or ""))
    return ""


def _call_log_row(log: LLMCallLog) -> CallLogRow:
    return CallLogRow(
        id=log.id,
        created_at=log.created_at,
        stage=log.stage,
        provider_name=log.provider_name,
        model=log.model,
        duration_ms=log.duration_ms,
        status=log.status,
        error=log.error,
        prompt_tokens=log.prompt_tokens,
        completion_tokens=log.completion_tokens,
        user_id=log.user_id,
        project_id=log.project_id,
        voyage_id=log.voyage_id,
        request_preview=_request_preview(log.request),
        response_preview=_preview(log.response or ""),
    )


@router.get("/call-logs/settings", response_model=CallLogSettings)
async def get_call_log_settings(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(current_active_user),
) -> CallLogSettings:
    return CallLogSettings(enabled=await llm_admin_service.get_call_logging_enabled(session))


@router.put("/call-logs/settings", response_model=CallLogSettings)
async def put_call_log_settings(
    data: CallLogSettings,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(current_active_user),
) -> CallLogSettings:
    enabled = await llm_admin_service.set_call_logging_enabled(session, data.enabled)
    return CallLogSettings(enabled=enabled)


@router.get("/call-logs", response_model=CallLogPage)
async def list_call_logs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    stage: str | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(current_active_user),
) -> CallLogPage:
    total, rows = await llm_admin_service.list_call_logs(
        session, stage=stage, limit=limit, offset=offset
    )
    return CallLogPage(total=total, items=[_call_log_row(r) for r in rows])


@router.delete("/call-logs")
async def clear_call_logs(
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(current_active_user),
) -> dict[str, int]:
    deleted = await llm_admin_service.clear_call_logs(session)
    return {"deleted": deleted}


@router.get("/call-logs/{log_id}", response_model=CallLogDetail)
async def get_call_log(
    log_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    _user: User = Depends(current_active_user),
) -> CallLogDetail:
    log = await llm_admin_service.get_call_log(session, log_id)
    if log is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="CALL_LOG_NOT_FOUND")
    return CallLogDetail.model_validate(log, from_attributes=True)
